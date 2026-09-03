"""Diagnostics support for the JetKVM integration (redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics (never password, cookies, host, or image bytes)."""
    runtime = entry.runtime_data or {}
    coord = runtime.get("device")
    device_registry = dr.async_get(hass)
    devices = [
        {
            "name": device.name,
            "model": device.model,
            "identifiers": [f"{d[0]}:{d[1]}" for d in device.identifiers],
        }
        for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    ]
    data = dict(entry.data)
    data.pop("password", None)
    return {
        "config": {
            "data": {**data, "host": "REDACTED"},
            "options": dict(entry.options),
        },
        "devices": devices,
        "device": {
            "last_update_success": coord.last_update_success if coord else None,
            "data": coord.data if coord else None,
        },
    }
