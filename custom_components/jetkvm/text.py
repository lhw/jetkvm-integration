"""Text platform: type text on the JetKVM host."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import JetKVMCoordinator, send_text


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the type-text entity."""
    async_add_entities([JetKVMTypeText(hass, entry)])


class JetKVMTypeText(TextEntity):
    """Text input that types its value via keypressReport."""

    _attr_has_entity_name = True
    _attr_name = "Type text"
    _attr_native_min = 1
    _attr_native_max = 256
    _attr_mode = "text"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._coord: JetKVMCoordinator = entry.runtime_data["device"]
        self._attr_native_value = None
        device_id = self._coord.device_id
        self._attr_unique_id = f"{device_id}-type-text"
        self._attr_device_info = {
            "identifiers": {("mqtt", device_id)},  # adopt the MQTT device as base
            "name": "JetKVM",
        }

    async def async_set_value(self, value: str) -> None:
        """Type the value on the host."""
        await send_text(self._coord, value)
        self._attr_native_value = value
        self.async_write_ha_state()
