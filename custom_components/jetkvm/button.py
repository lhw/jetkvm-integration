"""Button platform: Ctrl+Alt+Del + Wake-on-LAN."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_WOL_MAC
from .coordinator import JetKVMCoordinator, _press_ctrl_alt_del, wake_host


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up buttons."""
    entities: list[ButtonEntity] = [JetKVMCtrlAltDel(hass, entry)]
    wol_mac = (entry.options or entry.data).get(CONF_WOL_MAC)
    if wol_mac:
        entities.append(JetKVMWake(hass, entry))
    async_add_entities(entities)


class _BaseButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, name: str, suffix: str
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._coord: JetKVMCoordinator = entry.runtime_data["device"]
        self._attr_name = name
        device_id = self._coord.device_id
        self._attr_unique_id = f"{device_id}-{suffix}"
        self._attr_device_info = {
            "identifiers": {("mqtt", device_id)},  # adopt the MQTT device as base
            "name": "JetKVM",
        }


class JetKVMCtrlAltDel(_BaseButton):
    """Send Ctrl+Alt+Del via keyboardReport taps."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "Send Ctrl+Alt+Del", "ctrl-alt-del")

    async def async_press(self) -> None:
        await _press_ctrl_alt_del(self._coord)


class JetKVMWake(_BaseButton):
    """Wake the host via HTTP WOL (no WebRTC needed)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "Wake host", "wake")

    async def async_press(self) -> None:
        mac = (self._entry.options or self._entry.data).get(CONF_WOL_MAC)
        await wake_host(self._coord, mac)
