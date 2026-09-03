"""Camera platform: JetKVM screen as a polling camera."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    RTCIceCandidateInit,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import JetKVMCoordinator, fetch_screenshot

LOGGER = logging.getLogger(__package__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the JetKVM screen camera."""
    async_add_entities([JetKVMScreenCamera(hass, entry)])


class JetKVMScreenCamera(Camera):
    """Polling camera with native WebRTC passthrough for live view."""

    _attr_has_entity_name = True
    _attr_name = "Screen"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__()
        self.hass = hass
        self._entry = entry
        self._coord: JetKVMCoordinator = entry.runtime_data["device"]
        device_id = self._coord.device_id
        self._attr_unique_id = f"{device_id}-screen"
        self._attr_device_info = {
            # ("mqtt", …) merges our entities onto the MQTT-discovered device.
            "identifiers": {("mqtt", device_id)},  # adopt the MQTT device as base
            "name": "JetKVM",
            "manufacturer": "JetKVM",
            "model": "JetKVM",
            "configuration_url": entry.data.get("host"),
        }
        self._image: bytes | None = None
        self._attr_available = True

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a fresh screen image (captured on demand, not polled)."""
        try:
            self._image = await fetch_screenshot(self._coord)
            self._attr_available = True
        except Exception:
            LOGGER.exception("Screenshot failed")
            self._attr_available = False
        return self._image

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Proxy the frontend offer to the device (SDP passthrough).

        ponytail: the device keeps one session; a live view and a polling
        screenshot kick each other. Live view wins while open.
        """
        try:
            answer_sdp = await self._coord.exchange_offer(offer_sdp)
        except Exception as err:
            send_message(WebRTCError("jetkvm_webrtc_offer_failed", str(err)))
            return
        send_message(WebRTCAnswer(answer_sdp))

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """No-op: legacy HTTP signaling has no trickle; LAN host candidates suffice."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Image size only — never image bytes."""
        return {"bytes": len(self._image) if self._image else 0}
