# JetKVM for Home Assistant

Companion to JetKVM's native MQTT mode. MQTT already covers telemetry — this adds
what MQTT can't: **screen image + text-to-keyboard** via the device's WebRTC API
(same API the open-source web UI and `recorder-for-jetkvm` use).

- **Screen camera** — still image captured on demand (dashboard view, `camera.snapshot`
  service) plus native live view (WebRTC passthrough), each via an ephemeral device session.
- **Type text** — text entity types ASCII on the host (`keypressReport`).
- **Buttons** — Ctrl+Alt+Del + Wake-on-LAN (`POST /device/send-wol/:mac`).
- **One diagnostic sensor** — device ID. Everything else stays in MQTT to avoid duplicates.

> Note: the device keeps a single WebRTC session, so an open live view and a
> screenshot request kick each other — the live view wins while open.

## Installation

### HACS

1. In HACS, add this repository as a custom repository.
2. Install **JetKVM**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/jetkvm` into your HA `custom_components/` directory, restart.

## Configuration

Settings → Devices & Services → Add Integration → JetKVM: host (e.g. `http://192.168.1.168`),
password, optional WOL MAC, polling interval for device availability
(min 10s, default 30s).

If the JetKVM already reports to your MQTT broker with HA discovery enabled,
the integration is discovered automatically: the retained
`homeassistant/sensor/jetkvm_<id>/ip_address/config` message carries the device
IP, so the flow only asks for the device password. Either way our entities join
the *same* device card as the MQTT entities (shared `mqtt` identifier) —
nothing is duplicated.

## Development

```bash
uv sync
uv run pytest
uv run ruff check custom_components tests
```

Tests mock HTTP (`aioresponses`) and WebRTC; no device needed.
