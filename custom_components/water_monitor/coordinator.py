"""DataUpdateCoordinator for BVK Water Monitor."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BVK_LOGIN_URL,
    BVK_PLACE_LIST_URL,
    CONF_PASSWORD,
    CONF_SUEZ_TOKEN_URL,
    CONF_USERNAME,
    DEFAULT_UPDATE_INTERVAL,
    SUEZ_BASE_URL,
    SUEZ_DAILY_URL,
    SUEZ_HOME_URL,
)

_LOGGER = logging.getLogger(__name__)

# BVK form field names (ASP.NET server controls)
_BVK_FIELD_EMAIL = "ctl00$ctl00$lvLoginForm$LoginDialog1$edEmail"
_BVK_FIELD_PASSWORD = "ctl00$ctl00$lvLoginForm$LoginDialog1$edPassword"
_BVK_FIELD_SUBMIT = "ctl00$ctl00$lvLoginForm$LoginDialog1$btnLogin"


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

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return HA's shared aiohttp session (handles SSL/proxy correctly)."""
        return async_get_clientsession(self.hass)

    async def async_shutdown(self) -> None:
        """Clean up when the integration is unloaded."""
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # BVK portal helpers
    # ------------------------------------------------------------------

    async def _bvk_login(self, session: aiohttp.ClientSession) -> bool:
        """Log in to the BVK portal. Returns True on success."""
        # 1) GET the login page to collect ASP.NET hidden fields
        try:
            async with session.get(BVK_LOGIN_URL, timeout=aiohttp.ClientTimeout(total=30)) as resp:
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

        # 2) POST credentials
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

        # Successful login shows a logout link ("Odhlášení")
        if "Odhlášení" not in html and "odhlaseni" not in html.lower():
            raise ConfigEntryAuthFailed(
                "BVK login failed — check your email and password"
            )
        return True

    async def _bvk_find_suez_token_url(self, session: aiohttp.ClientSession) -> str | None:
        """
        After BVK login, navigate to ConsumptionPlaceList and attempt to extract
        the SUEZ smart-meter token URL.  Returns None if not found.
        """
        try:
            async with session.get(
                BVK_PLACE_LIST_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError:
            return None

        # Look for a direct SUEZ URL in the page
        match = re.search(
            r"(https://cz-sitr\.suezsmartsolutions\.com/eMIS\.SE_BVK/Login\.aspx\?token=[^\s\"'&]+)",
            html,
        )
        if match:
            return match.group(1)

        # Check hidden field hfCSCPT which may contain the token URL or just the token
        cscpt = _extract_hidden(html, "ctl00_ctl00_ContentPlaceHolder1Common_ContentPlaceHolder1_hfCSCPT")
        if cscpt and cscpt.startswith("http"):
            return cscpt

        return None

    # ------------------------------------------------------------------
    # SUEZ portal helpers
    # ------------------------------------------------------------------

    async def _suez_authenticate(self, session: aiohttp.ClientSession) -> bool:
        """Authenticate with the SUEZ portal using the stored token URL."""
        token_url = self._suez_token_url
        if not token_url:
            raise ConfigEntryAuthFailed("No SUEZ token URL configured")
        try:
            async with session.get(
                token_url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"SUEZ authentication failed: {exc}") from exc

        # A successful SUEZ login lands on Site.aspx (or a data page)
        if "Site_Energie" in str(resp.url) or "Site.aspx" in str(resp.url) or "Energie" in html:
            return True
        if "Login.aspx" in str(resp.url):
            raise ConfigEntryAuthFailed("SUEZ token URL is invalid or has expired")
        return True

    async def _suez_get_html(self, session: aiohttp.ClientSession, url: str) -> str:
        """Fetch a SUEZ page, re-authenticating once if redirected to login."""
        async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            resp.raise_for_status()
            final_url = str(resp.url)
            html = await resp.text()

        if "Login.aspx" in final_url:
            # Session expired — re-authenticate and retry once
            _LOGGER.debug("SUEZ session expired, re-authenticating")
            await self._suez_authenticate(session)
            async with session.get(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=30)) as resp2:
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

        # Meter index: animation code pattern "if (val > 710.93800) val = 710.93800;"
        m = re.search(r"val > (\d+\.\d+)", html)
        if m:
            data["meter_index_m3"] = float(m.group(1))

        # Today's total from hourly chart title:
        # "Křivka spotřeby <span ...>DD.MM.YYYY</span> &bull; <span ...>N l</span>"
        m = re.search(
            r"jqPlot_Eau.*?&bull;.*?TitreCouleur[^>]*>(\d+(?:[,\.]\d+)?)\s*([lm³]+)</span>",
            html,
            re.DOTALL,
        )
        if m:
            value_str = m.group(1).replace(",", ".")
            unit = m.group(2).strip()
            value = float(value_str)
            # Convert m³ to litres for consistency
            data["today_l"] = round(value * 1000, 1) if "m" in unit else value

        # Monthly total from CourbeMois chart title:
        # "Denní spotřeba za <span ...>month year</span> &bull; <span ...>N l</span>"
        m = re.search(
            r"CourbeMois_Eau.*?&bull;.*?TitreCouleur[^>]*>(\d+(?:[,\.]\d+)?)\s*([lm³]+)</span>",
            html,
            re.DOTALL,
        )
        if m:
            value_str = m.group(1).replace(",", ".")
            unit = m.group(2).strip()
            value = float(value_str)
            data["monthly_l"] = round(value * 1000, 1) if "m" in unit else value

        # Last reading timestamp
        m = re.search(r"Poslední odečet z\s*<span[^>]*>([^<]+)</span>", html)
        if m:
            data["last_reading_at"] = m.group(1).strip()

        return data

    @staticmethod
    def _parse_daily_page(html: str) -> dict[str, Any]:
        """Extract per-day consumption from Site_Energie.aspx?Affichage=ConsoJour."""
        data: dict[str, Any] = {}

        # HTML table rows: <td class="TableauEnergieLabel">DD.MM.YYYY</td>
        #                   <td ...><span class='CouleurConsommationEau'>N</span></td>
        pairs = re.findall(
            r'<td class="TableauEnergieLabel">(\d{2}\.\d{2}\.\d{4})</td>'
            r'<td[^>]*>.*?<span[^>]*>(\d+(?:[,\.]\d+)?)</span>',
            html,
            re.DOTALL,
        )

        if pairs:
            # Most recent entry is the last row
            most_recent_date, most_recent_val = pairs[-1]
            data["daily_date"] = most_recent_date
            data["daily_l"] = float(most_recent_val.replace(",", "."))

            if len(pairs) >= 2:
                prev_date, prev_val = pairs[-2]
                data["yesterday_date"] = prev_date
                data["yesterday_l"] = float(prev_val.replace(",", "."))

            # Full daily history as dict {date: litres}
            data["daily_history"] = {d: float(v.replace(",", ".")) for d, v in pairs}

        return data

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the latest water consumption data."""
        session = await self._get_session()

        # Ensure SUEZ session is active (re-auth on each coordinator refresh to be safe)
        # The token URL is long-lived, so calling it on every poll is acceptable but
        # may create a new server-side session.  We only re-auth when we detect expiry.
        try:
            home_html = await self._suez_get_html(session, SUEZ_HOME_URL)
            daily_html = await self._suez_get_html(session, SUEZ_DAILY_URL)
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"Failed to reach SUEZ portal: {exc}") from exc

        home_data = self._parse_home_page(home_html)
        daily_data = self._parse_daily_page(daily_html)

        if not home_data and not daily_data:
            raise UpdateFailed("SUEZ pages returned no parseable data")

        result: dict[str, Any] = {**home_data, **daily_data}

        # Prefer daily_l from the daily page; fall back to today_l from home page
        if "daily_l" not in result and "today_l" in result:
            result["daily_l"] = result["today_l"]

        _LOGGER.debug("BVK water data: %s", result)
        return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_hidden(html: str, field_id: str) -> str | None:
    """Extract value of an ASP.NET hidden field by name or id."""
    # Try by name attribute first
    m = re.search(
        rf'<input[^>]+name="{re.escape(field_id)}"[^>]+value="([^"]*)"',
        html,
    )
    if m:
        return m.group(1)
    # Try by id attribute
    m = re.search(
        rf'<input[^>]+id="{re.escape(field_id)}"[^>]+value="([^"]*)"',
        html,
    )
    if m:
        return m.group(1)
    # Some inputs have value before id
    m = re.search(
        rf'name="{re.escape(field_id)}"[^>]*value="([^"]*)"',
        html,
    )
    return m.group(1) if m else None
