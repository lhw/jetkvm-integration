"""Data coordinator + ephemeral WebRTC client for JetKVM.

Auth (verified live): POST /auth/login-local {"password"} sets a session cookie.
**Each login revokes the previous token** (verified: token1 → 401 after a token2
login), so the coordinator caches ONE token and re-auths lazily only on 401 —
otherwise the /device poll and screenshot/offer calls rotate and revoke each
other's tokens. Video/keys go over WebRTC: POST /webrtc/session exchanges a
base64 offer/answer, keys are JSON-RPC on the `rpc` DataChannel.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
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

    async def exchange_offer(self, offer_sdp: str) -> str:
        """Exchange a client offer for the device answer (token cached + retry)."""
        payload = base64.b64encode(
            json.dumps({"type": "offer", "sdp": offer_sdp}).encode()
        ).decode()
        for attempt in (0, 1):
            async with self.session.post(
                self.base_url + "/webrtc/session",
                headers=await self._headers(),
                json={"sd": payload},
                timeout=CLIENT_TIMEOUT,
            ) as resp:
                if resp.status == 401 and attempt == 0:
                    await self._revalidate()
                    continue
                resp.raise_for_status()
                answer = json.loads(
                    base64.b64decode((await resp.json())["sd"]).decode()
                )
                return answer["sdp"]
        raise UpdateFailed("JetKVM could not establish a WebRTC session")


def create_coordinators(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Build coordinators for an entry (stored on ``entry.runtime_data``)."""
    return {"device": JetKVMCoordinator(hass, entry)}


# ponytail: WebRTC below uses the coordinator's cached token + ephemeral sessions.
# Persistent peer connection when live-stream demand arrives.


async def _fetch_screenshot_stream(coord: JetKVMCoordinator) -> bytes:
    """Grab one JPEG via an ephemeral WebRTC session."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    pc = RTCPeerConnection()
    try:
        frames: asyncio.Queue = asyncio.Queue(maxsize=1)

        # Register BEFORE setRemoteDescription: aiortc emits "track" during
        # that call, and a later listener would miss it forever.
        @pc.on("track")
        def on_track(track):
            async def recv() -> None:
                while True:
                    frame = await track.recv()
                    if frames.empty():
                        frames.put_nowait(frame)
                        return

            asyncio.ensure_future(recv())  # noqa: RUF006 (fire-and-forget reader)

        pc.addTransceiver("video")
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        while pc.iceGatheringState != "complete":  # noqa: ASYNC110 (ICE gather poll)
            await asyncio.sleep(0.05)
        answer_sdp = await coord.exchange_offer(pc.localDescription.sdp)
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type="answer")
        )
        frame = await asyncio.wait_for(frames.get(), timeout=15)
        return _encode_jpeg(frame)
    finally:
        await pc.close()


def _encode_jpeg(frame) -> bytes:
    """Encode an aiortc VideoFrame to JPEG (Pillow ships with HA core)."""
    buf = io.BytesIO()
    frame.to_image().save(buf, format="JPEG")
    return buf.getvalue()


async def fetch_screenshot(coord: JetKVMCoordinator) -> bytes:
    """Take a screenshot using the coordinator's cached session token."""
    return await _fetch_screenshot_stream(coord)


async def _webrtc_rpc_channel(coord: JetKVMCoordinator):
    """Open an ephemeral WebRTC session, return (pc, open rpc channel)."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    pc = RTCPeerConnection()
    chan = pc.createDataChannel("rpc")
    ready = asyncio.Event()

    @chan.on("open")
    def on_open() -> None:
        ready.set()

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    while pc.iceGatheringState != "complete":  # noqa: ASYNC110 (ICE gather poll)
        await asyncio.sleep(0.05)
    answer_sdp = await coord.exchange_offer(pc.localDescription.sdp)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
    await asyncio.wait_for(ready.wait(), timeout=10)
    return pc, chan


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
    pc, chan = await _webrtc_rpc_channel(coord)
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
    pc, chan = await _webrtc_rpc_channel(coord)
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
        await pc.close()


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
