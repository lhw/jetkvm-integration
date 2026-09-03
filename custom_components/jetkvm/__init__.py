"""The JetKVM integration (companions native MQTT mode)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .coordinator import create_coordinators

PLATFORMS = ["camera", "text", "button", "sensor"]

LOGGER = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up JetKVM from a config entry."""
    coordinators = create_coordinators(hass, entry)
    entry.runtime_data = coordinators
    await coordinators["device"].async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry.runtime_data = None
    return unloaded
