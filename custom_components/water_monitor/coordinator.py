"""DataUpdateCoordinator for BVK Water Monitor."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BVK_LOGIN_URL,
    BVK_PLACE_LIST_URL,
    CONF_PASSWORD,
    CONF_SUEZ_TOKEN_URL,
    CONF_USERNAME,
    DEFAULT_UPDATE_INTERVAL,
    SUEZ_DAILY_URL,
    SUEZ_HOME_URL,
)

_LOGGER = logging.getLogger(__name__)

# BVK form field names (ASP.NET server controls)
_BVK_FIELD_EMAIL = "ctl00$ctl00$lvLoginForm$LoginDialog1$edEmail"
_BVK_FIELD_PASSWORD = "ctl00$ctl00$lvLoginForm$LoginDialog1$edPassword"
_BVK_FIELD_SUBMIT = "ctl00$ctl00$lvLoginForm$LoginDialog1$btnLogin"

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HA-water-monitor/1.0)"}


class BVKWaterCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that fetches water consumption data from BVK / SUEZ portals."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="BVK Water Monitor",
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )
        self._username: str = entry.data[CONF_USERNAME]
        self._password: str = entry.data[CONF_PASSWORD]
        self._suez_token_url: str = entry.data[CONF_SUEZ_TOKEN_URL]
        # Dedicated session with its own cookie jar so SUEZ auth cookies
        # are isolated and not shared with other HA integrations.
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        """Return (or lazily create) the dedicated aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                headers=_HEADERS,
            )
        return self._session

    async def async_shutdown(self) -> None:
        """Close the dedicated HTTP session on unload."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # BVK portal helpers
    # ------------------------------------------------------------------

    async def _bvk_login(self, session: aiohttp.ClientSession) -> bool:
        """Log in to the BVK portal. Returns True on success."""
        try:
            async with session.get(
                BVK_LOGIN_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"BVK login page fetch failed: {exc}") from exc

        viewstate = _extract_hidden(html, "__VIEWSTATE")
        vsgen = _extract_hidden(html, "__VIEWSTATEGENERATOR")
        prevpage = _extract_hidden(html, "__PREVIOUSPAGE")
        eventval = _extract_hidden(html, "__EVENTVALIDATION")

        if not viewstate:
            raise UpdateFailed("Could not parse BVK login page (missing __VIEWSTATE)")

        payload = {
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": vsgen or "",
            "__PREVIOUSPAGE": prevpage or "",
            "__EVENTVALIDATION": eventval or "",
            _BVK_FIELD_EMAIL: self._username,
            _BVK_FIELD_PASSWORD: self._password,
            _BVK_FIELD_SUBMIT: "Vstoupit",
            "ctl00$ctl00$captchaToken": "",
            "ctl00$ctl00$crs": "",
        }
        try:
            async with session.post(
                BVK_LOGIN_URL,
                data=payload,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"BVK login POST failed: {exc}") from exc

        if "Odhlášení" not in html and "odhlaseni" not in html.lower():
            raise ConfigEntryAuthFailed(
                "BVK login failed — check your email and password"
            )
        return True

    async def _bvk_find_suez_token_url(self, session: aiohttp.ClientSession) -> str | None:
        """After BVK login, try to extract the SUEZ token URL from ConsumptionPlaceList."""
        try:
            async with session.get(
                BVK_PLACE_LIST_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError:
            return None

        match = re.search(
            r"(https://cz-sitr\.suezsmartsolutions\.com/eMIS\.SE_BVK/Login\.aspx\?token=[^\s\"'&]+)",
            html,
        )
        if match:
            return match.group(1)

        cscpt = _extract_hidden(
            html,
            "ctl00_ctl00_ContentPlaceHolder1Common_ContentPlaceHolder1_hfCSCPT",
        )
        if cscpt and cscpt.startswith("http"):
            return cscpt

        return None

    # ------------------------------------------------------------------
    # SUEZ portal helpers
    # ------------------------------------------------------------------

    async def _suez_authenticate(self, session: aiohttp.ClientSession) -> None:
        """Authenticate with SUEZ using the stored token URL, setting session cookies."""
        if not self._suez_token_url:
            raise ConfigEntryAuthFailed("No SUEZ token URL configured")
        try:
            async with session.get(
                self._suez_token_url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                final_url = str(resp.url)
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"SUEZ authentication request failed: {exc}") from exc

        if "Login.aspx" in final_url and "token=" not in final_url:
            raise ConfigEntryAuthFailed(
                "SUEZ token URL is invalid or has expired — "
                "please reconfigure the integration with a fresh token URL"
            )
        _LOGGER.debug("SUEZ authenticated, landed on %s", final_url)

    async def _suez_get_html(self, session: aiohttp.ClientSession, url: str) -> str:
        """
        Fetch a SUEZ page.  If the response is the login page (session expired),
        re-authenticate once and retry.
        """
        async with session.get(
            url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            final_url = str(resp.url)
            html = await resp.text()

        if "Login.aspx" in final_url:
            _LOGGER.debug("SUEZ session expired, re-authenticating before %s", url)
            await self._suez_authenticate(session)
            async with session.get(
                url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp2:
                resp2.raise_for_status()
                html = await resp2.text()

        return html

    # ------------------------------------------------------------------
    # Data parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_home_page(html: str) -> dict[str, Any]:
        """Extract meter index, today's consumption and monthly total from Site.aspx."""
        data: dict[str, Any] = {}

        # Meter index: JS animation "if (val > 710.93800) val = 710.93800;"
        m = re.search(r"val > (\d+\.\d+)", html)
        if m:
            data["meter_index_m3"] = float(m.group(1))

        # Today's total: "Křivka spotřeby … &bull; <span …>N l</span>"
        m = re.search(
            r"jqPlot_Eau.*?&bull;.*?TitreCouleur[^>]*>(\d+(?:[,\.]\d+)?)\s*([lm³]+)</span>",
            html,
            re.DOTALL,
        )
        if m:
            value = float(m.group(1).replace(",", "."))
            data["today_l"] = round(value * 1000, 1) if "m" in m.group(2) else value

        # Monthly total: "Denní spotřeba za … &bull; <span …>N l</span>"
        m = re.search(
            r"CourbeMois_Eau.*?&bull;.*?TitreCouleur[^>]*>(\d+(?:[,\.]\d+)?)\s*([lm³]+)</span>",
            html,
            re.DOTALL,
        )
        if m:
            value = float(m.group(1).replace(",", "."))
            data["monthly_l"] = round(value * 1000, 1) if "m" in m.group(2) else value

        # Last reading timestamp
        m = re.search(r"Poslední odečet z\s*<span[^>]*>([^<]+)</span>", html)
        if m:
            data["last_reading_at"] = m.group(1).strip()

        return data

    @staticmethod
    def _parse_daily_page(html: str) -> dict[str, Any]:
        """Extract per-day consumption from Site_Energie.aspx?Affichage=ConsoJour."""
        data: dict[str, Any] = {}

        pairs = re.findall(
            r'<td class="TableauEnergieLabel">(\d{2}\.\d{2}\.\d{4})</td>'
            r'<td[^>]*>.*?<span[^>]*>(\d+(?:[,\.]\d+)?)</span>',
            html,
            re.DOTALL,
        )
        if pairs:
            most_recent_date, most_recent_val = pairs[-1]
            data["daily_date"] = most_recent_date
            data["daily_l"] = float(most_recent_val.replace(",", "."))

            if len(pairs) >= 2:
                prev_date, prev_val = pairs[-2]
                data["yesterday_date"] = prev_date
                data["yesterday_l"] = float(prev_val.replace(",", "."))

            data["daily_history"] = {d: float(v.replace(",", ".")) for d, v in pairs}

        return data

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Authenticate with SUEZ (if needed) then fetch consumption data."""
        session = self._get_session()

        # Always authenticate on each cycle to ensure a fresh SUEZ session.
        # The token URL is long-lived; the round-trip is ~200 ms.
        try:
            await self._suez_authenticate(session)
            home_html = await self._suez_get_html(session, SUEZ_HOME_URL)
            daily_html = await self._suez_get_html(session, SUEZ_DAILY_URL)
        except ConfigEntryAuthFailed:
            raise
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"Network error reaching SUEZ portal: {exc}") from exc

        home_data = self._parse_home_page(home_html)
        daily_data = self._parse_daily_page(daily_html)

        if not home_data and not daily_data:
            raise UpdateFailed("SUEZ pages returned no parseable data")

        result: dict[str, Any] = {**home_data, **daily_data}
        if "daily_l" not in result and "today_l" in result:
            result["daily_l"] = result["today_l"]

        _LOGGER.debug("BVK water data: %s", result)
        return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_hidden(html: str, field_id: str) -> str | None:
    """Extract value of an ASP.NET hidden field by name or id."""
    for attr in ("name", "id"):
        m = re.search(
            rf'<input[^>]+{attr}="{re.escape(field_id)}"[^>]+value="([^"]*)"',
            html,
        )
        if m:
            return m.group(1)
    m = re.search(rf'name="{re.escape(field_id)}"[^>]*value="([^"]*)"', html)
    return m.group(1) if m else None
