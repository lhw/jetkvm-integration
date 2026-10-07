"""Data coordinator + ephemeral WebRTC client for JetKVM.

Auth (verified live): POST /auth/login-local {"password"} sets a session cookie.
**Each login revokes the previous token** (verified: token1 → 401 after a token2
login), so the coordinator caches ONE token and re-auths lazily only on 401 —
otherwise the /device poll and screenshot/offer calls rotate and revoke each
other's tokens. Video/keys go over WebRTC, signaled over the device's
``ws://…/webrtc/signaling/client`` WebSocket (newer firmware removed the legacy
HTTP ``/webrtc/session`` endpoint); keys are JSON-RPC on the `rpc` DataChannel.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
from contextlib import suppress
from datetime import timedelta
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_HOST, CONF_PASSWORD, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

LOGGER = logging.getLogger(__package__)
CLIENT_TIMEOUT = aiohttp.ClientTimeout(total=10, sock_connect=3)


def _signaling_url(base_url: str) -> str:
    """Return the device WebSocket signaling URL.

    Newer firmware dropped the legacy HTTP ``/webrtc/session`` endpoint; all
    signaling now goes over this WebSocket.
    """
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :] + "/webrtc/signaling/client"
    return "ws://" + base_url.split("://", 1)[-1] + "/webrtc/signaling/client"


class _WebRTCSession:
    """A live WebRTC session: peer connection + signaling WS + background tasks."""

    def __init__(self, pc, ws, reader: asyncio.Task, ping: asyncio.Task) -> None:
        self.pc = pc
        self.ws = ws
        self._tasks = (reader, ping)

    async def aclose(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        await self.ws.close()
        await self.pc.close()


async def _send_candidate(ws: aiohttp.ClientWebSocketResponse, candidate) -> None:
    """Send a local ICE candidate to the device over the signaling WS."""
    from aiortc.sdp import candidate_to_sdp

    with suppress(aiohttp.ClientError):
        await ws.send_json(
            {
                "type": "new-ice-candidate",
                "data": {
                    "candidate": candidate_to_sdp(candidate),
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                },
            }
        )


async def _keepalive(ws: aiohttp.ClientWebSocketResponse) -> None:
    """Ping the signaling WS so the device keeps the session open."""
    try:
        while True:
            await asyncio.sleep(15)
            await ws.send_str("ping")
    except (aiohttp.ClientError, asyncio.CancelledError):
        pass


async def login_token(
    session: aiohttp.ClientSession, base_url: str, password: str
) -> str:
    """Log in and return just the authToken (device sets it with no Domain)."""
    async with session.post(
        base_url + "/auth/login-local",
        json={"password": password},
        timeout=CLIENT_TIMEOUT,
    ) as resp:
        if resp.status in (401, 403):
            raise UpdateFailed("Invalid password")
        resp.raise_for_status()
        jar = SimpleCookie()
        for header in resp.headers.getall("Set-Cookie", []):
            jar.load(header)
    if "authToken" not in jar:
        raise UpdateFailed("Login did not return a session cookie")
    return jar["authToken"].value


async def _auth_headers(
    session: aiohttp.ClientSession, base_url: str, password: str
) -> dict[str, str]:
    """One-shot login → Cookie header. Used only by the config-flow probe."""
    token = await login_token(session, base_url, password)
    return {"Cookie": f"authToken={token}"}


class JetKVMCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll /device for availability + device info. Max backoff 5 min.

    Also owns the cached authToken (with an asyncio lock) so screenshots,
    live-view offers, WOL, and typing can reuse it instead of each logging in
    (and revoking the others).
    """

    MAX_RETRY_AFTER = 300.0

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        entry_data = entry.data
        self.base_url = entry_data[CONF_HOST].rstrip("/")
        scan_interval = (entry.options or entry_data).get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"jetkvm {self.base_url}",
            update_interval=timedelta(seconds=int(scan_interval)),
        )
        self.session = async_get_clientsession(hass)
        self._base_interval = float(scan_interval)
        self._fail_streak = 0
        self._token: str | None = None
        self._token_lock = asyncio.Lock()

    @property
    def device_id(self) -> str | None:
        """The device's stable id (falls back to entry_id only if unknown)."""
        device = (self.data or {}).get("device") or {}
        return device.get("deviceId") or self.config_entry.entry_id

    def _next_retry_after(self) -> float:
        self._fail_streak += 1
        return min(
            self.MAX_RETRY_AFTER, self._base_interval * 2 ** (self._fail_streak - 1)
        )

    async def _headers(self) -> dict[str, str]:
        """Return a Cookie header, logging in lazily (token cached)."""
        async with self._token_lock:
            if self._token is None:
                self._token = await login_token(
                    self.session, self.base_url, self._password()
                )
            return {"Cookie": f"authToken={self._token}"}

    async def _revalidate(self) -> None:
        """Drop the cached token (device may have logged in elsewhere)."""
        async with self._token_lock:
            self._token = None

    def _password(self) -> str:
        return (self.config_entry.options or self.config_entry.data).get(
            CONF_PASSWORD, ""
        )

    async def _async_update_data(self) -> dict[str, Any]:
        base = self.base_url
        try:
            device = await self._request_device()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise UpdateFailed(
                f"Error communicating with {base}: {err}",
                retry_after=self._next_retry_after(),
            ) from err
        if isinstance(device, dict) and device.get("deviceId"):
            self._fail_streak = 0
        else:
            self._fail_streak += 1
        return {"device": device if isinstance(device, dict) else {}}

    async def _request_device(self) -> Any:
        """GET /device with token cache + one 401 retry."""
        for attempt in (0, 1):
            async with self.session.get(
                self.base_url + "/device",
                headers=await self._headers(),
                timeout=CLIENT_TIMEOUT,
            ) as resp:
                if resp.status == 401 and attempt == 0:
                    await self._revalidate()
                    continue
                resp.raise_for_status()
                return await resp.json()
        return {}

    async def ws_connect_signaling(self) -> aiohttp.ClientWebSocketResponse:
        """Connect the device's WebSocket signaling channel (auth cookie)."""
        return await self.session.ws_connect(
            _signaling_url(self.base_url),
            headers=await self._headers(),
            timeout=CLIENT_TIMEOUT,
        )

    async def connect(self, pc) -> _WebRTCSession:
        """Signaling over the device WebSocket; returns a session to keep open.

        Sends a full (non-trickle) offer so our ICE candidates ride in the SDP,
        and adds the device's trickled candidates as they arrive.
        """
        from aiortc import RTCSessionDescription
        from aiortc.sdp import candidate_from_sdp

        ws = await self.ws_connect_signaling()
        answer: asyncio.Future = asyncio.get_running_loop().create_future()

        @pc.on("icecandidate")
        def _on_ice(event) -> None:
            if event.candidate is not None:
                asyncio.ensure_future(_send_candidate(ws, event.candidate))

        async def _reader() -> None:
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT or msg.data == "pong":
                        continue
                    try:
                        payload = json.loads(msg.data)
                    except ValueError:
                        continue
                    kind = payload.get("type")
                    if kind == "answer" and not answer.done():
                        answer.set_result(payload["data"])
                    elif kind == "new-ice-candidate":
                        data = payload.get("data") or {}
                        sdp = data.get("candidate")
                        if sdp:
                            candidate = candidate_from_sdp(sdp)
                            candidate.sdpMid = data.get("sdpMid")
                            candidate.sdpMLineIndex = data.get("sdpMLineIndex")
                            await pc.addIceCandidate(candidate)
            except (aiohttp.ClientError, asyncio.CancelledError):
                pass

        reader = asyncio.ensure_future(_reader())
        try:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)
            while pc.iceGatheringState != "complete":  # noqa: ASYNC110 (ICE gather poll)
                await asyncio.sleep(0.05)
            payload = base64.b64encode(
                json.dumps({"type": "offer", "sdp": pc.localDescription.sdp}).encode()
            ).decode()
            await ws.send_json({"type": "offer", "data": {"sd": payload}})
            answer_b64 = await asyncio.wait_for(answer, timeout=15)
            answer_sdp = json.loads(base64.b64decode(answer_b64).decode())
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=answer_sdp["sdp"], type=answer_sdp["type"])
            )
        except Exception:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
            await ws.close()
            raise
        ping = asyncio.ensure_future(_keepalive(ws))
        return _WebRTCSession(pc, ws, reader, ping)


def create_coordinators(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Build coordinators for an entry (stored on ``entry.runtime_data``)."""
    return {"device": JetKVMCoordinator(hass, entry)}


# ponytail: WebRTC below uses the coordinator's cached token + ephemeral sessions.
# Persistent peer connection when live-stream demand arrives.


async def _fetch_screenshot_stream(coord: JetKVMCoordinator) -> bytes:
    """Grab one JPEG via an ephemeral WebRTC session."""
    from aiortc import RTCPeerConnection

    pc = RTCPeerConnection()
    frames: asyncio.Queue = asyncio.Queue(maxsize=1)

    # Register BEFORE the offer/answer: aiortc emits "track" during
    # setRemoteDescription, and a later listener would miss it forever.
    @pc.on("track")
    def on_track(track):
        async def recv() -> None:
            while True:
                frame = await track.recv()
                if frames.empty():
                    frames.put_nowait(frame)
                    return

        asyncio.ensure_future(recv())  # noqa: RUF006 (fire-and-forget reader)

    # The JetKVM encodes H264; pin the transceiver to H264 so the answer
    # doesn't negotiate VP8 (which would never decode).
    from aiortc import RTCRtpReceiver

    transceiver = pc.addTransceiver("video")
    transceiver.setCodecPreferences(
        [
            codec
            for codec in RTCRtpReceiver.getCapabilities("video").codecs
            if codec.mimeType.lower() == "video/h264"
        ]
    )
    session = await coord.connect(pc)
    try:
        frame = await asyncio.wait_for(frames.get(), timeout=20)
        return _encode_jpeg(frame)
    finally:
        await session.aclose()


def _encode_jpeg(frame) -> bytes:
    """Encode an aiortc VideoFrame to JPEG (Pillow ships with HA core)."""
    buf = io.BytesIO()
    frame.to_image().save(buf, format="JPEG")
    return buf.getvalue()


async def fetch_screenshot(coord: JetKVMCoordinator) -> bytes:
    """Take a screenshot using the coordinator's cached session token."""
    return await _fetch_screenshot_stream(coord)


async def _webrtc_rpc_channel(coord: JetKVMCoordinator):
    """Open an ephemeral WebRTC session, return (session, open rpc channel)."""
    from aiortc import RTCPeerConnection

    pc = RTCPeerConnection()
    chan = pc.createDataChannel("rpc")
    ready = asyncio.Event()

    @chan.on("open")
    def on_open() -> None:
        ready.set()

    session = await coord.connect(pc)
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
    except Exception:
        await session.aclose()
        raise
    return session, chan


async def send_text(coord: JetKVMCoordinator, text: str) -> None:
    """Type ASCII text via keypressReport over an ephemeral WebRTC rpc channel."""
    await _send_key_steps(coord, _text_to_keys(text))


async def _press_ctrl_alt_del(coord: JetKVMCoordinator) -> None:
    """Send Ctrl+Alt+Del as one keyboardReport press + release."""
    await _send_keyboard_report(coord, modifier=0x05, keys=[0x4C])


async def _send_key_steps(
    coord: JetKVMCoordinator, steps: list[tuple[int, bool | None, bool]]
) -> None:
    """Send keypressReport steps over an ephemeral WebRTC rpc channel."""
    session, chan = await _webrtc_rpc_channel(coord)
    try:
        msg_id = 0
        for key, press, release_after in steps:
            for press_flag in (True, False) if release_after else (press,):
                msg_id += 1
                chan.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "keypressReport",
                            "params": {"key": key, "press": press_flag},
                            "id": msg_id,
                        }
                    )
                )
                await asyncio.sleep(0.02)
    finally:
        await pc.close()


async def _send_keyboard_report(
    coord: JetKVMCoordinator, modifier: int, keys: list[int]
) -> None:
    """Send one keyboardReport press + release (for combos like Ctrl+Alt+Del)."""
    session, chan = await _webrtc_rpc_channel(coord)
    try:
        for mod, ks in ((modifier, keys), (0, [])):
            chan.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "keyboardReport",
                        "params": {"modifier": mod, "keys": ks},
                        "id": 1 if ks else 2,
                    }
                )
            )
            await asyncio.sleep(0.05)
    finally:
        await session.aclose()


async def wake_host(coord: JetKVMCoordinator, mac: str) -> None:
    """Wake the host via HTTP WOL (no WebRTC needed)."""
    for attempt in (0, 1):
        async with coord.session.post(
            f"{coord.base_url}/device/send-wol/{mac}",
            headers=await coord._headers(),
            timeout=CLIENT_TIMEOUT,
        ) as resp:
            if resp.status == 401 and attempt == 0:
                await coord._revalidate()
                continue
            resp.raise_for_status()


# ponytail: US layout subset; extend when a non-US user complains.
_TEXT_MAP = {
    c: (ord(c.upper()) - ord("A") + 4, c.isupper())
    for c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
}
_TEXT_MAP.update(
    {d: (ord(d) - ord("1") + 30 if d != "0" else 39, False) for d in "1234567890"}
)
_TEXT_MAP.update(
    {" ": (44, False), "\n": (40, False), "-": (45, False), "=": (46, False)}
)

# Named keys → USB HID usage IDs (press/release combos like F-keys, Enter, arrows).
_NAMED_KEYS: dict[str, int] = {
    "F1": 0x3A,
    "F2": 0x3B,
    "F3": 0x3C,
    "F4": 0x3D,
    "F5": 0x3E,
    "F6": 0x3F,
    "F7": 0x40,
    "F8": 0x41,
    "F9": 0x42,
    "F10": 0x44,
    "F11": 0x45,
    "F12": 0x46,
    "ENTER": 0x28,
    "ESC": 0x29,
    "TAB": 0x2B,
    "DEL": 0x4C,
    "BACKSPACE": 0x2A,
    "UP": 0x52,
    "DOWN": 0x51,
    "LEFT": 0x50,
    "RIGHT": 0x4F,
    "CTRL": 0xE0,
    "ALT": 0xE2,
    "SHIFT": 0xE1,
}


def resolve_key(key: str | int) -> int:
    """Resolve a key name (e.g. 'F1', 'ENTER') or a HID code to an int."""
    if isinstance(key, int):
        return key
    upper = str(key).strip().upper()
    if upper in _NAMED_KEYS:
        return _NAMED_KEYS[upper]
    if upper.isdigit():
        return int(upper)
    if upper in _TEXT_MAP and len(upper) == 1:
        return _TEXT_MAP[upper][0]
    raise ValueError(f"Unknown key: {key}")


def _text_to_keys(text: str) -> list[tuple[int, bool | None, bool]]:
    """Map text to (hid_key, press, release_after) steps. Unknown chars skipped."""
    steps: list[tuple[int, bool | None, bool]] = []
    for char in text[:256]:
        mapped = _TEXT_MAP.get(char)
        if mapped is None:
            continue
        key, shift = mapped
        if shift:
            steps.append((0xE1, True, False))  # left shift down
            steps.append((key, None, True))  # tap key
            steps.append((0xE1, False, False))  # left shift up
        else:
            steps.append((key, None, True))
    return steps
