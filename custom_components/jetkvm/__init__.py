"""The JetKVM integration (companions native MQTT mode)."""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import _send_keyboard_report, create_coordinators, resolve_key

PLATFORMS = ["camera", "text", "button", "sensor"]

LOGGER = logging.getLogger(__package__)

SEND_KEYS_SCHEMA = vol.Schema(
    {
        vol.Required("keys"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("modifier", default=0): cv.positive_int,
        vol.Optional("delay", default=0.05): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=10)
        ),
    }
)


async def async_setup(hass: HomeAssistant, config: dict | None = None) -> bool:
    """Register the send_keys service (runs once, applies to all entries)."""
    hass.services.async_register(
        DOMAIN, "send_keys", _service_send_keys, SEND_KEYS_SCHEMA
    )
    return True


async def _service_send_keys(call: ServiceCall) -> None:
    """Send a list of HID keys (named or decimal codes) to every JetKVM entry."""
    hass = call.hass
    keys = call.data["keys"]
    modifier = call.data["modifier"]
    delay = call.data["delay"]
    for entry in hass.config_entries.async_entries(DOMAIN):
        coord = (entry.runtime_data or {}).get("device")
        if coord is None:
            continue
        for key in keys:
            code = resolve_key(key)
            await _send_keyboard_report(coord, modifier, [code])
            if delay:
                await asyncio.sleep(delay)


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
