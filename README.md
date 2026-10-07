# JetKVM for Home Assistant

Companion to JetKVM's native MQTT mode. MQTT already covers telemetry — this adds
what MQTT can't: **screen image + text-to-keyboard** via the device's WebRTC API
(same API the open-source web UI and `recorder-for-jetkvm` use).

- **Screen camera** — still image captured on demand (dashboard view, `camera.snapshot`
  service) plus native live view (WebRTC passthrough), each via an ephemeral device session.
- **Type text** — text entity types ASCII on the host (`keypressReport`).
- **`jetkvm.send_keys` service** — press/release named keys (F1, F10, ENTER, …)
  or decimal HID codes; used for macros/sequences from automations.
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

If the JetKVM already reports to your MQTT broker, the integration is
discovered automatically: it subscribes to the device's own retained
`jetkvm/<id>/network/state` topic and reads the host IP from it, so the flow
only asks for the device password. Either way our entities join the *same*
device card as the MQTT entities (shared `mqtt` identifier) — nothing is
duplicated.

## Notes

The integration depends on a lightly-patched **aiortc** vendored in this repo at
[`vendor/aiortc`](vendor/aiortc) (the `av` upper-pin relaxed to `<20`). HA
2026.10 pins `av==19.0.0` for every integration, while stock aiortc declares
`av<18` — a conservative pin, since aiortc 1.15.0 runs fine on av 19 — so a
plain PyPI requirement is unsatisfiable. The manifest installs it from this
repo's `vendor/aiortc` subdirectory. When upstream aiortc allows av 19, delete
`vendor/` and go back to a normal `aiortc` requirement.

## Development

```bash
uv sync
uv run pytest
uv run ruff check custom_components tests
```

Tests mock HTTP (`aioresponses`) and WebRTC; no device needed.
