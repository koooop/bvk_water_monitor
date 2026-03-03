"""Config flow for BVK Water Monitor."""
from __future__ import annotations

import re
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BVK_LOGIN_URL,
    BVK_PLACE_LIST_URL,
    CONF_PASSWORD,
    CONF_SUEZ_TOKEN_URL,
    CONF_USERNAME,
    DOMAIN,
)
from .coordinator import _extract_hidden

# Validation pattern for the SUEZ token URL
_SUEZ_URL_PATTERN = re.compile(
    r"https://cz-sitr\.suezsmartsolutions\.com/eMIS\.SE_BVK/Login\.aspx\?token=.+"
)

_STEP1_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_STEP2_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SUEZ_TOKEN_URL): str,
    }
)


class WaterMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BVK Water Monitor."""

    VERSION = 1

    def __init__(self) -> None:
        self._username: str = ""
        self._password: str = ""
        self._suez_token_url: str = ""

    # ------------------------------------------------------------------
    # Step 1 – BVK credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]

            token_url, login_error = await self._try_bvk_login_and_detect_token(
                self._username, self._password
            )

            if login_error == "invalid_auth":
                errors["base"] = "invalid_auth"
            elif login_error == "cannot_connect":
                errors["base"] = "cannot_connect"
            elif token_url:
                # Auto-detected — skip step 2
                self._suez_token_url = token_url
                return await self._create_entry()
            else:
                # Logged in but no token URL found — ask user to provide it
                return await self.async_step_token()

        return self.async_show_form(
            step_id="user",
            data_schema=_STEP1_SCHEMA,
            errors=errors,
            description_placeholders={
                "bvk_url": "https://zis.bvk.cz",
            },
        )

    # ------------------------------------------------------------------
    # Step 2 – SUEZ token URL (when auto-detection fails)
    # ------------------------------------------------------------------

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            token_url = user_input[CONF_SUEZ_TOKEN_URL].strip()
            if not _SUEZ_URL_PATTERN.match(token_url):
                errors[CONF_SUEZ_TOKEN_URL] = "invalid_token_url"
            else:
                # Pattern is valid — accept it and let the coordinator
                # report authentication errors on first data fetch
                self._suez_token_url = token_url
                return await self._create_entry()

        return self.async_show_form(
            step_id="token",
            data_schema=_STEP2_SCHEMA,
            errors=errors,
            description_placeholders={
                "suez_instructions": (
                    "Log in to https://zis.bvk.cz, navigate to your consumption place, "
                    "click the smart meter icon, and copy the full URL from your browser."
                ),
            },
        )

    # ------------------------------------------------------------------
    # Re-authentication (token expired)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Triggered by ConfigEntryAuthFailed when the SUEZ token expires."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show form to collect a fresh SUEZ token URL."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token_url = user_input[CONF_SUEZ_TOKEN_URL].strip()
            if not _SUEZ_URL_PATTERN.match(token_url):
                errors[CONF_SUEZ_TOKEN_URL] = "invalid_token_url"
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={CONF_SUEZ_TOKEN_URL: token_url},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_STEP2_SCHEMA,
            errors=errors,
            description_placeholders={
                "suez_instructions": (
                    "Log in to https://zis.bvk.cz, navigate to your consumption place, "
                    "click the smart meter icon, and copy the full URL from your browser."
                ),
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _try_bvk_login_and_detect_token(
        self, username: str, password: str
    ) -> tuple[str | None, str | None]:
        """
        Attempt to log in to BVK and detect the SUEZ token URL automatically.
        Returns (token_url_or_None, error_key_or_None).
        """
        # Use HA's managed session so SSL/proxy settings are respected
        session = async_get_clientsession(self.hass)
        try:
            # GET login page to collect ASP.NET hidden fields
            async with session.get(
                BVK_LOGIN_URL, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()

            viewstate = _extract_hidden(html, "__VIEWSTATE")
            vsgen = _extract_hidden(html, "__VIEWSTATEGENERATOR")
            prevpage = _extract_hidden(html, "__PREVIOUSPAGE")
            eventval = _extract_hidden(html, "__EVENTVALIDATION")

            if not viewstate:
                return None, "cannot_connect"

            payload = {
                "__VIEWSTATE": viewstate,
                "__VIEWSTATEGENERATOR": vsgen or "",
                "__PREVIOUSPAGE": prevpage or "",
                "__EVENTVALIDATION": eventval or "",
                "ctl00$ctl00$lvLoginForm$LoginDialog1$edEmail": username,
                "ctl00$ctl00$lvLoginForm$LoginDialog1$edPassword": password,
                "ctl00$ctl00$lvLoginForm$LoginDialog1$btnLogin": "Vstoupit",
                "ctl00$ctl00$captchaToken": "",
                "ctl00$ctl00$crs": "",
            }

            async with session.post(
                BVK_LOGIN_URL,
                data=payload,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()

            if "Odhlášení" not in html:
                return None, "invalid_auth"

            # Try to find SUEZ token URL in ConsumptionPlaceList
            async with session.get(
                BVK_PLACE_LIST_URL, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()

            m = re.search(
                r"(https://cz-sitr\.suezsmartsolutions\.com/eMIS\.SE_BVK/Login\.aspx\?token=[^\s\"'&]+)",
                html,
            )
            if m:
                return m.group(1), None

            # Logged in but token URL not found in page — user must provide it
            return None, None

        except aiohttp.ClientConnectorError:
            return None, "cannot_connect"
        except aiohttp.ClientResponseError:
            return None, "cannot_connect"

    async def _create_entry(self) -> ConfigFlowResult:
        await self.async_set_unique_id(self._username.lower())
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"BVK Water ({self._username})",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_SUEZ_TOKEN_URL: self._suez_token_url,
            },
        )
