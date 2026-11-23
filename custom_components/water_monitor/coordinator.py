"""DataUpdateCoordinator for BVK Water Monitor."""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

import aiohttp
from yarl import URL
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_TOKEN_URL,
    DEFAULT_UPDATE_INTERVAL,
    SUEZ_BASE_URL,
    SUEZ_DAILY_URL,
)

_LOGGER = logging.getLogger(__name__)

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
        self._token_url: str = entry.data.get(CONF_TOKEN_URL, "")

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _new_session(self) -> aiohttp.ClientSession:
        """Create a fresh dedicated session with an isolated cookie jar."""
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            headers=_HEADERS,
        )

    # ------------------------------------------------------------------
    # SUEZ portal helpers
    # ------------------------------------------------------------------

    async def _suez_authenticate(
        self, session: aiohttp.ClientSession, token_url: str
    ) -> str:
        """Authenticate with SUEZ using the given token URL.

        Returns the effective SUEZ home URL (with RefSite parameter).
        """
        return await _async_suez_authenticate(session, token_url)

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
        """Authenticate with the saved token URL and fetch its consumption data."""
        if not _valid_token_url(self._token_url):
            raise ConfigEntryAuthFailed(
                "A valid SUEZ token URL is required; please reconfigure the integration."
            )

        session = self._new_session()
        try:
            home_url = await self._suez_authenticate(session, self._token_url)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(home_url).query)
            ref_site = query.get("RefSite", [""])[0]
            cp_id = ref_site or "default"
            daily_url = (
                f"{SUEZ_BASE_URL}/Site_Energie.aspx?"
                f"{urllib.parse.urlencode({'Affichage': 'ConsoJour', 'RefSite': ref_site})}"
                if ref_site
                else SUEZ_DAILY_URL
            )

            home_html = await self._suez_get_html(session, home_url, self._token_url)
            daily_html = await self._suez_get_html(session, daily_url, self._token_url)
        except ConfigEntryAuthFailed:
            raise
        except (UpdateFailed, aiohttp.ClientError) as exc:
            raise UpdateFailed(f"Failed to fetch SUEZ consumption data: {exc}") from exc
        finally:
            await session.close()

        place_data: dict[str, Any] = {
            **self._parse_home_page(home_html),
            **self._parse_daily_page(daily_html),
            "label": ref_site or "BVK",
        }
        if "daily_l" not in place_data and "today_l" in place_data:
            place_data["daily_l"] = place_data["today_l"]

        if not any(
            key in place_data for key in ("meter_index_m3", "daily_l", "monthly_l")
        ):
            raise UpdateFailed(
                "No consumption data was found for the configured SUEZ token URL"
            )

        _LOGGER.debug("BVK place %s data: %s", cp_id, place_data)
        return {cp_id: place_data}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_SUEZ_HOST = "cz-sitr.suezsmartsolutions.com"
_SUEZ_TOKEN_PATH = "/eMIS.SE_BVK/Login.aspx"


def _valid_token_url(token_url: str) -> bool:
    """Return whether the supplied URL is a safe SUEZ token URL."""
    try:
        if any(character.isspace() for character in token_url):
            return False
        parsed = urllib.parse.urlsplit(token_url)
        query = urllib.parse.parse_qs(parsed.query)
        token = query.get("token", [""])[0]
        return (
            parsed.scheme == "https"
            and parsed.hostname == _SUEZ_HOST
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path == _SUEZ_TOKEN_PATH
            and not parsed.fragment
            and bool(token.strip())
        )
    except ValueError:
        return False


async def _async_suez_authenticate(
    session: aiohttp.ClientSession, token_url: str
) -> str:
    """Authenticate at SUEZ and return the effective home URL."""
    if not _valid_token_url(token_url):
        raise ConfigEntryAuthFailed("The configured SUEZ token URL is invalid.")

    current_url = token_url
    headers = {
        **_HEADERS,
        "Referer": "https://zis.bvk.cz/ConsumptionPlaceList.aspx",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    }

    for _ in range(6):
        parsed = urllib.parse.urlsplit(current_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _SUEZ_HOST
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise UpdateFailed("SUEZ authentication redirected to an unexpected host")

        try:
            async with session.get(
                current_url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
                headers=headers,
            ) as resp:
                status = resp.status
                location = resp.headers.get("Location", "")
                await resp.text()
                _inject_cookies(session, resp)
        except aiohttp.ClientError:
            raise UpdateFailed("SUEZ authentication request failed") from None

        if status in (301, 302, 303, 307, 308) and location:
            current_url = urllib.parse.urljoin(current_url, location)
            continue

        if status == 200:
            if (
                urllib.parse.urlsplit(current_url).path.rsplit("/", 1)[-1].lower()
                != "login.aspx"
            ):
                return current_url
            raise ConfigEntryAuthFailed(
                "The SUEZ token URL is invalid or has expired; reconfigure the integration."
            )

        raise UpdateFailed(f"SUEZ authentication returned HTTP {status}")

    raise UpdateFailed("SUEZ authentication exceeded the redirect limit")


def _inject_cookies(
    session: aiohttp.ClientSession, resp: aiohttp.ClientResponse
) -> None:
    """Work around aiohttp's duplicate-Set-Cookie bug.

    When a server sends the same cookie name twice (e.g. once with an expired
    date to clear an old value, then again with the real value), aiohttp's
    SimpleCookie-based jar keeps only the first occurrence.  This helper
    re-parses every Set-Cookie header in order and forcefully updates the jar
    with the last non-expired value for each cookie name.
    """
    final: dict[str, str] = {}
    for header in resp.headers.getall("Set-Cookie", []):
        parts = [p.strip() for p in header.split(";")]
        if not parts or "=" not in parts[0]:
            continue
        name, _, value = parts[0].partition("=")
        # A cookie with a past expiry date is a deletion instruction — skip it
        is_deletion = any(
            p.lower().startswith("expires=") and (
                "1999" in p or "2000" in p or "2001" in p or "2002" in p
            )
            for p in parts[1:]
        )
        if not is_deletion and value:
            final[name.strip()] = value.strip()

    if final:
        session.cookie_jar.update_cookies(final, URL(str(resp.url)))
