"""Sensor platform: one diagnostic device sensor (MQTT owns the rest)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import JetKVMCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the device-id sensor."""
    coordinator: JetKVMCoordinator = entry.runtime_data["device"]
    async_add_entities([JetKVMDeviceId(coordinator)])


class JetKVMDeviceId(CoordinatorEntity[JetKVMCoordinator], SensorEntity):
    """Device ID from /device (string sensor, diagnostic)."""

    _attr_has_entity_name = True
    _attr_name = "Device ID"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: JetKVMCoordinator) -> None:
        super().__init__(coordinator)
        device_id = coordinator.device_id
        self._attr_unique_id = f"{device_id}-device-id"
        self._attr_device_info = {
            "identifiers": {("mqtt", device_id)},  # adopt the MQTT device as base
            "name": "JetKVM",
            "manufacturer": "JetKVM",
            "model": "JetKVM",
        }

    @property
    def native_value(self) -> str | None:
        """Return the device ID."""
        return (self.coordinator.data.get("device") or {}).get("deviceId")

    @property
    def available(self) -> bool:
        """Follow coordinator availability."""
        return self.coordinator.last_update_success
