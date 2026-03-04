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
        # Fallback token URL (used only when BVK auto-detection fails)
        self._suez_token_url: str = entry.data.get(CONF_SUEZ_TOKEN_URL, "")
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
        """After BVK login, extract a fresh SUEZ token URL from BVK pages.

        Tries ConsumptionPlaceList first (direct URL, hidden field, postback),
        then falls back to the BVK home page.
        """
        pages_to_try = [
            ("ConsumptionPlaceList", BVK_PLACE_LIST_URL),
            ("home", BVK_LOGIN_URL),
        ]

        for page_name, url in pages_to_try:
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    resp.raise_for_status()
                    html = await resp.text()
            except aiohttp.ClientError as exc:
                _LOGGER.debug("BVK %s fetch failed: %s", page_name, exc)
                continue

            # Diagnostic: count references to help troubleshoot
            n_sitr = html.lower().count("cz-sitr")
            n_suez = html.lower().count("suez")
            n_token = html.count("token=")
            _LOGGER.debug(
                "BVK %s: length=%d  cz-sitr=%d  suez=%d  token==%d",
                page_name, len(html), n_sitr, n_suez, n_token,
            )
            for keyword in ("cz-sitr", "token="):
                idx = html.lower().find(keyword)
                if idx != -1:
                    snippet = " ".join(html[max(0, idx - 80):idx + 250].split())
                    _LOGGER.debug("BVK %s snippet[%s]: %s", page_name, keyword, snippet)

            # 1. Direct URL anywhere in HTML/JS
            match = re.search(
                r"(https://cz-sitr\.suezsmartsolutions\.com[^\s\"'<>\\]+)",
                html,
            )
            if match:
                return _html.unescape(match.group(1))

            # 2. Hidden field hfCSCPT
            cscpt = _extract_hidden(
                html,
                "ctl00_ctl00_ContentPlaceHolder1Common_ContentPlaceHolder1_hfCSCPT",
            )
            if cscpt and cscpt.startswith("http"):
                return _html.unescape(cscpt)

            # 3. Postback-based redirect (only on ConsumptionPlaceList)
            if url == BVK_PLACE_LIST_URL:
                postback_targets = re.findall(r"__doPostBack\('([^']+)'", html)
                _LOGGER.debug("BVK %s postback targets: %s", page_name, postback_targets)
                suez_targets = [
                    t for t in postback_targets
                    if any(kw in t.lower() for kw in ("suez", "smart", "odber"))
                ]
                if suez_targets:
                    token_url = await self._bvk_postback_redirect(
                        session, html, suez_targets[0]
                    )
                    if token_url:
                        return token_url

        return None

    async def _bvk_postback_redirect(
        self,
        session: aiohttp.ClientSession,
        page_html: str,
        event_target: str,
    ) -> str | None:
        """Simulate an ASP.NET postback and capture the SUEZ token URL from the redirect."""
        payload = {
            "__VIEWSTATE": _extract_hidden(page_html, "__VIEWSTATE") or "",
            "__VIEWSTATEGENERATOR": _extract_hidden(page_html, "__VIEWSTATEGENERATOR") or "",
            "__PREVIOUSPAGE": _extract_hidden(page_html, "__PREVIOUSPAGE") or "",
            "__EVENTVALIDATION": _extract_hidden(page_html, "__EVENTVALIDATION") or "",
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": "",
        }
        try:
            async with session.post(
                BVK_PLACE_LIST_URL,
                data=payload,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                location = resp.headers.get("Location", "")
                _LOGGER.debug(
                    "BVK postback(%s): status=%d  Location=%s",
                    event_target, resp.status, location,
                )
                if "cz-sitr" in location and "token=" in location:
                    return location
        except aiohttp.ClientError as exc:
            _LOGGER.debug("BVK postback failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # SUEZ portal helpers
    # ------------------------------------------------------------------

    async def _suez_authenticate(
        self, session: aiohttp.ClientSession, token_url: str
    ) -> str:
        """Authenticate with SUEZ using the given token URL.

        Returns the effective SUEZ home URL (may include a RefSite parameter
        derived from the authentication redirect chain).
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
            # Landed directly on a data page — authentication complete
            return final_url

        # We landed on Login.aspx. Two cases:
        # A) ReturnUrl is present → token was valid; SUEZ needs one more GET to
        #    complete the session handshake (standard ASP.NET Forms Auth pattern).
        # B) No ReturnUrl and no token= in URL → token is genuinely invalid/expired.
        return_url_raw = re.search(r"[?&]ReturnUrl=([^&\s]+)", final_url)
        if not return_url_raw:
            raise ConfigEntryAuthFailed(
                "SUEZ token URL is invalid or has expired — "
                "please reconfigure the integration with a fresh token URL"
            )

        # Follow the ReturnUrl to complete authentication
        return_path = urllib.parse.unquote(return_url_raw.group(1))
        if return_path.startswith("/"):
            return_url = "https://cz-sitr.suezsmartsolutions.com" + return_path
        else:
            return_url = return_path
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
            raise ConfigEntryAuthFailed(
                "SUEZ token URL is invalid or has expired — "
                "please reconfigure the integration with a fresh token URL"
            )

        # Authentication complete; return the confirmed data-page URL
        return final_url2

    async def _suez_get_html(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token_url: str = "",
    ) -> str:
        """
        Fetch a SUEZ page.  If the response is the login page (session expired),
        re-authenticate once using token_url and retry.
        """
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
        """Log into BVK for a fresh SUEZ token, authenticate, then fetch consumption data."""

        # Start each poll cycle with a clean session so there are no stale cookies
        # from a previous cycle that could interfere with authentication.
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = self._new_session()
        session = self._session

        # --- Step 1: BVK login → extract fresh SUEZ token URL ---
        suez_token_url: str | None = None
        try:
            await self._bvk_login(session)
            suez_token_url = await self._bvk_find_suez_token_url(session)
            if suez_token_url:
                _LOGGER.debug("Got fresh SUEZ token URL via BVK portal")
            else:
                _LOGGER.debug(
                    "BVK login succeeded but SUEZ token URL not found in page; "
                    "falling back to stored token URL"
                )
        except ConfigEntryAuthFailed:
            raise
        except UpdateFailed as exc:
            _LOGGER.debug("BVK auto-detection failed (%s); trying stored token URL", exc)

        # Fall back to the token URL stored during config (may be empty)
        if not suez_token_url:
            suez_token_url = self._suez_token_url or None

        if not suez_token_url:
            raise UpdateFailed(
                "No SUEZ token URL available. BVK auto-detection failed and no stored "
                "token URL is configured. Please reconfigure the integration."
            )

        # --- Step 2: SUEZ authenticate and fetch data ---
        try:
            await self._suez_authenticate(session, suez_token_url)
            home_html = await self._suez_get_html(session, SUEZ_HOME_URL, suez_token_url)
            daily_html = await self._suez_get_html(session, SUEZ_DAILY_URL, suez_token_url)
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
