"""Camera platform: JetKVM screen as a polling camera."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import suppress
from typing import Any

import aiohttp
from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    RTCIceCandidateInit,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import JetKVMCoordinator, fetch_screenshot

LOGGER = logging.getLogger(__package__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the JetKVM screen camera."""
    async_add_entities([JetKVMScreenCamera(hass, entry)])


class _SignalingRelay:
    """Relay a browser WebRTC session to the device's WebSocket signaling channel."""

    def __init__(
        self, coord: JetKVMCoordinator, offer_sdp: str, send_message: WebRTCSendMessage
    ) -> None:
        self._coord = coord
        self._offer = offer_sdp
        self._send = send_message
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None

    async def start(self) -> None:
        """Connect signaling, start the reader, and forward the browser offer."""
        self._ws = await self._coord.ws_connect_signaling()
        self._reader = asyncio.ensure_future(self._read())
        payload = base64.b64encode(
            json.dumps({"type": "offer", "sdp": self._offer}).encode()
        ).decode()
        await self._ws.send_json({"type": "offer", "data": {"sd": payload}})

    async def _read(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT or msg.data == "pong":
                    continue
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    continue
                kind = payload.get("type")
                if kind == "answer":
                    answer = json.loads(base64.b64decode(payload["data"]).decode())
                    self._send(WebRTCAnswer(answer["sdp"]))
                elif kind == "new-ice-candidate":
                    data = payload.get("data") or {}
                    if data.get("candidate"):
                        self._send(
                            WebRTCCandidate(
                                RTCIceCandidateInit(
                                    candidate=data["candidate"],
                                    sdp_mid=data.get("sdpMid"),
                                    sdp_m_line_index=data.get("sdpMLineIndex"),
                                )
                            )
                        )
        except (aiohttp.ClientError, asyncio.CancelledError):
            pass

    async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
        """Forward a browser ICE candidate to the device."""
        if self._ws is None:
            return
        with suppress(aiohttp.ClientError):
            await self._ws.send_json(
                {
                    "type": "new-ice-candidate",
                    "data": {
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdp_mid,
                        "sdpMLineIndex": candidate.sdp_m_line_index,
                    },
                }
            )

    async def aclose(self) -> None:
        """Tear down the relay."""
        if self._reader is not None:
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
        if self._ws is not None:
            await self._ws.close()


class JetKVMScreenCamera(Camera):
    """On-demand stills + native WebRTC live view (relayed to the device)."""

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
        self._relays: dict[str, _SignalingRelay] = {}

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
        """Relay the browser offer to the device over its WebSocket signaling.

        ponytail: the device keeps one session; a live view and a polling
        screenshot kick each other. Live view wins while open.
        """
        try:
            relay = _SignalingRelay(self._coord, offer_sdp, send_message)
            await relay.start()
        except Exception as err:  # noqa: BLE001
            send_message(WebRTCError("jetkvm_webrtc_offer_failed", str(err)))
            return
        self._relays[session_id] = relay

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward a browser ICE candidate to the device."""
        if relay := self._relays.get(session_id):
            await relay.send_candidate(candidate)

    @callback
    def async_close_session(self, session_id: str) -> None:
        """Close a live-view relay."""
        if relay := self._relays.pop(session_id, None):
            self.hass.async_create_task(relay.aclose())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Image size only — never image bytes."""
        return {"bytes": len(self._image) if self._image else 0}
