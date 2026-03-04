"""Config flow for BVK Water Monitor."""
from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BVK_LOGIN_URL,
    BVK_PLACE_LIST_URL,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)
from .coordinator import _extract_hidden

_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class WaterMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BVK Water Monitor."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single-step setup: validate BVK credentials and save."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            error = await self._validate_credentials(username, password)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(username.lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"BVK Water ({username})",
                    data={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_CREDENTIALS_SCHEMA,
            errors=errors,
            description_placeholders={"bvk_url": "https://zis.bvk.cz"},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Triggered by ConfigEntryAuthFailed (wrong BVK credentials)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enter BVK credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            error = await self._validate_credentials(username, password)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_CREDENTIALS_SCHEMA,
            errors=errors,
        )

    async def _validate_credentials(self, username: str, password: str) -> str | None:
        """Try BVK login and verify at least one consumption place exists.

        Returns an error key string on failure, or None on success.
        """
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                BVK_LOGIN_URL, timeout=aiohttp.ClientTimeout(total=20)
            ) as resp:
                resp.raise_for_status()
                html = await resp.text()

            viewstate = _extract_hidden(html, "__VIEWSTATE")
            if not viewstate:
                return "cannot_connect"

            payload = {
                "__VIEWSTATE": viewstate,
                "__VIEWSTATEGENERATOR": _extract_hidden(html, "__VIEWSTATEGENERATOR") or "",
                "__PREVIOUSPAGE": _extract_hidden(html, "__PREVIOUSPAGE") or "",
                "__EVENTVALIDATION": _extract_hidden(html, "__EVENTVALIDATION") or "",
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
                return "invalid_auth"

        except aiohttp.ClientConnectorError:
            return "cannot_connect"
        except aiohttp.ClientResponseError:
            return "cannot_connect"

        return None
