"""DataUpdateCoordinator for BVK Water Monitor."""
from __future__ import annotations

import html as _html
import logging
import re
import urllib.parse
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

# Use a realistic browser User-Agent so portals return full page content
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


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
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _new_session(self) -> aiohttp.ClientSession:
        """Create a fresh dedicated session with an isolated cookie jar."""
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            headers=_HEADERS,
        )

    async def async_shutdown(self) -> None:
        """Close the dedicated HTTP session on unload."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # BVK portal helpers
    # ------------------------------------------------------------------

    async def _bvk_login(self, session: aiohttp.ClientSession) -> None:
        """Log in to the BVK portal."""
        try:
            async with session.get(
                BVK_LOGIN_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"BVK login page fetch failed: {exc}") from exc

        viewstate = _extract_hidden(html, "__VIEWSTATE")
        if not viewstate:
            raise UpdateFailed("Could not parse BVK login page (missing __VIEWSTATE)")

        payload = {
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": _extract_hidden(html, "__VIEWSTATEGENERATOR") or "",
            "__PREVIOUSPAGE": _extract_hidden(html, "__PREVIOUSPAGE") or "",
            "__EVENTVALIDATION": _extract_hidden(html, "__EVENTVALIDATION") or "",
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

    async def _bvk_get_all_places(
        self, session: aiohttp.ClientSession
    ) -> list[dict[str, str]]:
        """After BVK login, enumerate all consumption places and get fresh SUEZ token URLs.

        Returns a list of dicts: {cp_id, cp_num, token_url}.
        """
        try:
            async with session.get(
                BVK_PLACE_LIST_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"BVK ConsumptionPlaceList fetch failed: {exc}") from exc

        # Find Show$N postback targets from HTML-encoded onclick attributes.
        # The page renders onclick as: onclick="javascript:__doPostBack(&#39;target&#39;,&#39;Show$0&#39;)"
        seen: set[int] = set()
        show_targets: list[tuple[str, str]] = []
        for onclick in re.findall(r'onclick=["\']([^"\']+)["\']', html):
            decoded = _html.unescape(onclick)
            m = re.search(r"__doPostBack\('([^']+)','(Show\$(\d+))'", decoded)
            if m:
                idx = int(m.group(3))
                if idx not in seen:
                    seen.add(idx)
                    show_targets.append((m.group(1), m.group(2)))

        _LOGGER.debug("BVK found %d consumption place(s)", len(show_targets))

        if not show_targets:
            return []

        places: list[dict[str, str]] = []
        for event_target, event_arg in show_targets:
            payload = {
                "__VIEWSTATE": _extract_hidden(html, "__VIEWSTATE") or "",
                "__VIEWSTATEGENERATOR": _extract_hidden(html, "__VIEWSTATEGENERATOR") or "",
                "__VIEWSTATEENCRYPTED": "",
                "__EVENTVALIDATION": _extract_hidden(html, "__EVENTVALIDATION") or "",
                "__EVENTTARGET": event_target,
                "__EVENTARGUMENT": event_arg,
            }
            try:
                async with session.post(
                    BVK_PLACE_LIST_URL,
                    data=payload,
                    allow_redirects=True,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    resp.raise_for_status()
                    detail_html = await resp.text()
            except aiohttp.ClientError as exc:
                _LOGGER.warning("BVK postback %s failed: %s", event_arg, exc)
                continue

            cp_id = _extract_input(detail_html, "edCpId") or event_arg.replace("$", "_")
            cp_num = (_extract_input(detail_html, "edCpEvNum") or cp_id).strip()

            # Extract fresh SUEZ token URL — the first href pointing to cz-sitr
            m = re.search(r'href="(https://cz-sitr[^"]+)"', detail_html)
            if not m:
                _LOGGER.warning("BVK %s: no SUEZ token URL found in MainInfo page", event_arg)
                continue

            token_url = _html.unescape(m.group(1))
            places.append({"cp_id": cp_id, "cp_num": cp_num, "token_url": token_url})
            _LOGGER.debug("BVK place %s (OM %s): token URL found", cp_id, cp_num)

        return places

    # ------------------------------------------------------------------
    # SUEZ portal helpers
    # ------------------------------------------------------------------

    async def _suez_authenticate(
        self, session: aiohttp.ClientSession, token_url: str
    ) -> str:
        """Authenticate with SUEZ using the given token URL.

        Returns the effective SUEZ home URL (with RefSite parameter).
        Raises UpdateFailed or ConfigEntryAuthFailed on error.
        """
        _LOGGER.debug("SUEZ auth: requesting %s", token_url[:80])
        _suez_headers = {
            "Referer": "https://zis.bvk.cz/ConsumptionPlaceList.aspx",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
        }
        try:
            async with session.get(
                token_url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
                headers=_suez_headers,
            ) as resp:
                resp.raise_for_status()
                final_url = str(resp.url)
                await resp.text()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"SUEZ authentication request failed: {exc}") from exc

        _LOGGER.debug("SUEZ auth: step-1 landed on %s", final_url)

        final_path = final_url.split("?")[0]
        if not final_path.endswith("Login.aspx"):
            return final_url

        # Landed on Login.aspx — follow the ReturnUrl to complete ASP.NET Forms Auth
        return_url_raw = re.search(r"[?&]ReturnUrl=([^&\s]+)", final_url)
        if not return_url_raw:
            raise ConfigEntryAuthFailed(
                "SUEZ token URL is invalid or has expired — please reconfigure"
            )

        return_path = urllib.parse.unquote(return_url_raw.group(1))
        return_url = (
            "https://cz-sitr.suezsmartsolutions.com" + return_path
            if return_path.startswith("/")
            else return_path
        )
        _LOGGER.debug("SUEZ auth: following ReturnUrl %s", return_url)

        try:
            async with session.get(
                return_url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=30),
                headers=_suez_headers,
            ) as resp2:
                resp2.raise_for_status()
                final_url2 = str(resp2.url)
                await resp2.text()
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"SUEZ ReturnUrl request failed: {exc}") from exc

        _LOGGER.debug("SUEZ auth: step-2 landed on %s", final_url2)

        if final_url2.split("?")[0].endswith("Login.aspx"):
            raise UpdateFailed(
                "SUEZ authentication did not complete — the portal may be temporarily unavailable"
            )

        return final_url2

    async def _suez_get_html(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token_url: str = "",
    ) -> str:
        """Fetch a SUEZ page, re-authenticating once if the session has expired."""
        async with session.get(
            url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            final_url = str(resp.url)
            html = await resp.text()

        if "Login.aspx" in final_url and token_url:
            _LOGGER.debug("SUEZ session expired, re-authenticating before %s", url)
            await self._suez_authenticate(session, token_url)
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

        m = re.search(r"val > (\d+\.\d+)", html)
        if m:
            data["meter_index_m3"] = float(m.group(1))

        m = re.search(
            r"jqPlot_Eau.*?&bull;.*?TitreCouleur[^>]*>(\d+(?:[,\.]\d+)?)\s*([lm³]+)</span>",
            html,
            re.DOTALL,
        )
        if m:
            value = float(m.group(1).replace(",", "."))
            data["today_l"] = round(value * 1000, 1) if "m" in m.group(2) else value

        m = re.search(
            r"CourbeMois_Eau.*?&bull;.*?TitreCouleur[^>]*>(\d+(?:[,\.]\d+)?)\s*([lm³]+)</span>",
            html,
            re.DOTALL,
        )
        if m:
            value = float(m.group(1).replace(",", "."))
            data["monthly_l"] = round(value * 1000, 1) if "m" in m.group(2) else value

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
        """Log into BVK, get fresh SUEZ tokens per place, fetch consumption data."""

        # Fresh session for the BVK login + place discovery phase
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = self._new_session()
        session = self._session

        # Step 1: BVK login → enumerate all consumption places
        await self._bvk_login(session)
        places = await self._bvk_get_all_places(session)

        if not places:
            raise UpdateFailed(
                "No consumption places found in the BVK portal. "
                "Please verify your account has registered meters."
            )

        # Step 2: For each place, authenticate to SUEZ and fetch data
        result: dict[str, Any] = {}
        for place in places:
            place_session = self._new_session()
            try:
                home_url = await self._suez_authenticate(place_session, place["token_url"])

                # Use the same RefSite the auth landed on for the daily page too
                ref_site_m = re.search(r"RefSite=([^&]+)", home_url)
                daily_url = (
                    f"{SUEZ_BASE_URL}/Site_Energie.aspx?Affichage=ConsoJour&RefSite={ref_site_m.group(1)}"
                    if ref_site_m
                    else SUEZ_DAILY_URL
                )

                home_html = await self._suez_get_html(place_session, home_url, place["token_url"])
                daily_html = await self._suez_get_html(place_session, daily_url, place["token_url"])
            except ConfigEntryAuthFailed:
                raise
            except (UpdateFailed, aiohttp.ClientError) as exc:
                _LOGGER.warning("Skipping place %s (%s): %s", place["cp_id"], place["cp_num"], exc)
                continue
            finally:
                await place_session.close()

            place_data: dict[str, Any] = {
                **self._parse_home_page(home_html),
                **self._parse_daily_page(daily_html),
            }
            place_data["label"] = place["cp_num"]
            if "daily_l" not in place_data and "today_l" in place_data:
                place_data["daily_l"] = place_data["today_l"]

            result[place["cp_id"]] = place_data
            _LOGGER.debug("BVK place %s data: %s", place["cp_id"], place_data)

        if not result:
            raise UpdateFailed("Failed to fetch data from any consumption place")

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


def _extract_input(html: str, field_suffix: str) -> str | None:
    """Extract input value by matching the last segment of its name attribute."""
    m = re.search(
        rf'name="[^"]*{re.escape(field_suffix)}"[^>]*value="([^"]*)"',
        html,
    )
    if not m:
        m = re.search(
            rf'value="([^"]*)"[^>]*name="[^"]*{re.escape(field_suffix)}"',
            html,
        )
    return m.group(1) if m else None
