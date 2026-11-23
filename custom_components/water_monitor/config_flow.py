"""Config flow for BVK Water Monitor."""
from __future__ import annotations

import hashlib
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import CONF_TOKEN_URL, DOMAIN
from .coordinator import _async_suez_authenticate, _valid_token_url

_TOKEN_URL_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN_URL): str})


class WaterMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BVK Water Monitor."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Set up the integration with a SUEZ token URL."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token_url = user_input[CONF_TOKEN_URL].strip()
            error = await self._validate_token_url(token_url)
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(_token_url_unique_id(token_url))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="BVK Water Monitor",
                    data={CONF_TOKEN_URL: token_url},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_TOKEN_URL_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when the SUEZ token URL is missing, invalid, or expired."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter a replacement SUEZ token URL."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token_url = user_input[CONF_TOKEN_URL].strip()
            error = await self._validate_token_url(token_url)
            if error:
                errors["base"] = error
            else:
                entry = self._get_reauth_entry()
                token_id = _token_url_unique_id(token_url)
                if self._token_url_configured(token_id, entry.entry_id):
                    return self.async_abort(reason="already_configured")
                self.hass.config_entries.async_update_entry(
                    entry, data={CONF_TOKEN_URL: token_url}, unique_id=token_id
                )
                return self.async_update_reload_and_abort(entry)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_TOKEN_URL_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the configured SUEZ token URL."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            token_url = user_input[CONF_TOKEN_URL].strip()
            error = await self._validate_token_url(token_url)
            if error:
                errors["base"] = error
            else:
                token_id = _token_url_unique_id(token_url)
                if self._token_url_configured(token_id, entry.entry_id):
                    return self.async_abort(reason="already_configured")
                self.hass.config_entries.async_update_entry(
                    entry, data={CONF_TOKEN_URL: token_url}, unique_id=token_id
                )
                return self.async_update_reload_and_abort(entry)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_TOKEN_URL_SCHEMA,
            errors=errors,
        )

    async def _validate_token_url(self, token_url: str) -> str | None:
        """Check the URL format and authenticate directly with SUEZ."""
        if not _valid_token_url(token_url):
            return "invalid_token_url"

        session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        try:
            await _async_suez_authenticate(session, token_url)
        except ConfigEntryAuthFailed:
            return "invalid_auth"
        except (UpdateFailed, aiohttp.ClientError):
            return "cannot_connect"
        finally:
            await session.close()

        return None

    def _token_url_configured(self, token_id: str, current_entry_id: str) -> bool:
        """Check for another entry using the same token without rejecting itself."""
        return any(
            entry.entry_id != current_entry_id and entry.unique_id == token_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )


def _token_url_unique_id(token_url: str) -> str:
    """Create a stable, non-secret config entry ID from the token."""
    from urllib.parse import parse_qs, urlsplit

    token = parse_qs(urlsplit(token_url).query)["token"][0]
    return hashlib.sha256(token.encode()).hexdigest()
