"""BVK Water Monitor integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN_URL, DOMAIN
from .coordinator import BVKWaterCoordinator

PLATFORMS = ["sensor"]
_LOGGER = logging.getLogger(__name__)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Remove obsolete BVK credentials when migrating to token URL auth."""
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_TOKEN_URL: ""},
            unique_id=f"legacy-{entry.entry_id}",
            version=2,
        )
        _LOGGER.warning(
            "BVK Water Monitor settings were migrated. Enter a SUEZ token URL "
            "to reconnect; stored BVK credentials have been removed."
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BVK Water Monitor from a config entry."""
    coordinator = BVKWaterCoordinator(hass, entry)

    # First refresh authenticates with SUEZ and fetches initial data.
    # ConfigEntryNotReady is raised automatically by async_config_entry_first_refresh
    # if UpdateFailed or ConfigEntryAuthFailed is thrown.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: BVKWaterCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok
