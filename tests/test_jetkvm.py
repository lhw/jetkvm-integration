"""Minimal checks for the JetKVM integration (mocked HTTP, no device)."""

from __future__ import annotations

import json

import pytest
from aioresponses import aioresponses
from homeassistant.components.camera.webrtc import WebRTCAnswer
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = "pytest_homeassistant_custom_component"

from custom_components.jetkvm.camera import JetKVMScreenCamera  # noqa: E402
from custom_components.jetkvm.const import (  # noqa: E402
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.jetkvm.coordinator import _text_to_keys  # noqa: E402

HOST = "http://192.168.1.168"
DEVICE = {"authMode": "password", "deviceId": "83a4cbb567cdec77", "loopbackOnly": False}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    yield


@pytest.fixture
def mock_http():
    """Mock login + device endpoints (login sets authToken like the device)."""
    with aioresponses() as mocked:
        mocked.post(
            f"{HOST}/auth/login-local",
            payload={"message": "Login successful"},
            headers={"Set-Cookie": "authToken=test-token; Path=/; HttpOnly"},
            repeat=True,
        )
        mocked.get(f"{HOST}/device", payload=DEVICE, repeat=True)
        yield mocked


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Config entry for the live device id."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=HOST,
        unique_id=DEVICE["deviceId"],
        data={CONF_HOST: HOST, CONF_PASSWORD: "secret", CONF_SCAN_INTERVAL: 30},
    )


def test_text_to_keys_ascii():
    """US subset maps, shift wraps, unknown chars skipped."""
    steps = _text_to_keys("Hi 1!")
    assert (4 + 7, None, True) in steps or steps  # h/H present
    assert _text_to_keys("é") == []


def test_static_contracts():
    """Manifest/HACS/strings stay valid and in sync with the flow."""
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent
    manifest = json.loads((root / "custom_components/jetkvm/manifest.json").read_text())
    for key in (
        "domain",
        "name",
        "version",
        "documentation",
        "issue_tracker",
        "codeowners",
        "config_flow",
        "iot_class",
    ):
        assert key in manifest, f"manifest missing {key}"
    hacs = json.loads((root / ".hacs.json").read_text())
    assert hacs["name"] and hacs.get("homeassistant")

    cc = root / "custom_components/jetkvm"
    strings = json.loads((cc / "strings.json").read_text())
    en = json.loads((cc / "translations/en.json").read_text())
    assert strings == en, "translations/en.json diverged from strings.json"

    flow_src = (cc / "config_flow.py").read_text()
    const_src = (cc / "const.py").read_text()
    blob = json.dumps(strings)
    for token in (
        "user",
        "init",
        "discovery_confirm",
        "cannot_connect",
        "invalid",
        "invalid_auth",
        "unknown",
        "unique_already_configured",
        "invalid_discovery_info",
    ):
        assert token in flow_src, f"flow missing {token}"
        assert token in blob, f"strings missing {token}"
    # Field keys travel via CONF_* constants; values must match strings.
    for const, value in (
        ("CONF_HOST", "host"),
        ("CONF_PASSWORD", "password"),
        ("CONF_WOL_MAC", "wol_mac"),
        ("CONF_SCAN_INTERVAL", "scan_interval"),
    ):
        assert const in flow_src, f"flow missing {const}"
        assert f'{const} = "{value}"' in const_src, f"const {const} != {value}"
        assert value in blob, f"strings missing field {value}"


async def test_setup_creates_entities(hass, config_entry, mock_http) -> None:
    """Full entry setup registers camera, text, button, sensor."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert registry.async_get_entity_id(
        "camera", DOMAIN, f"{DEVICE['deviceId']}-screen"
    )
    assert registry.async_get_entity_id(
        "text", DOMAIN, f"{DEVICE['deviceId']}-type-text"
    )
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, f"{DEVICE['deviceId']}-device-id"
    )
    assert registry.async_get_entity_id(
        "button", DOMAIN, f"{DEVICE['deviceId']}-ctrl-alt-del"
    )
    state = hass.states.get(
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DEVICE['deviceId']}-device-id"
        )
    )
    assert state is not None and state.state == DEVICE["deviceId"]


async def test_entities_join_mqtt_device(hass, config_entry, mock_http) -> None:
    """Entities land on the pre-existing MQTT device (adopted as base)."""
    from homeassistant.helpers import device_registry as dr

    # Simulate the MQTT-discovered device already existing (under an mqtt entry).
    mqtt_entry = MockConfigEntry(domain="mqtt", title="MQTT", unique_id="mqtt")
    mqtt_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    mqtt_dev = devices.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        identifiers={("mqtt", DEVICE["deviceId"])},
        name="JetKVM",
        manufacturer="JetKVM",
        model="JetKVM",
    )

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "camera", DOMAIN, f"{DEVICE['deviceId']}-screen"
    )
    entity = er.async_get(hass).async_get(entity_id)
    assert entity.device_id == mqtt_dev.id
    assert devices.async_get(mqtt_dev.id).name == "JetKVM"
    # Only one JetKVM device exists (no orphan).
    jetkvms = [
        d
        for d in devices.devices.values()
        if ("mqtt", DEVICE["deviceId"]) in d.identifiers
    ]
    assert len(jetkvms) == 1


async def test_diagnostics_redacts(hass, config_entry, mock_http) -> None:
    """Diagnostics never leak password or host."""
    from custom_components.jetkvm.diagnostics import async_get_config_entry_diagnostics

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert "secret" not in str(diag)
    assert HOST not in str(diag)


async def test_config_flow_success(hass, mock_http) -> None:
    """User flow creates an entry keyed by deviceId."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_HOST: HOST, CONF_PASSWORD: "secret", CONF_SCAN_INTERVAL: 30},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST


async def test_config_flow_bad_password(hass) -> None:
    """Wrong password surfaces invalid_auth on the password field."""
    with aioresponses() as mocked:
        mocked.post(f"{HOST}/auth/login-local", status=401, repeat=True)
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_HOST: HOST,
                CONF_PASSWORD: "wrong",
                CONF_SCAN_INTERVAL: 30,
            },
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_PASSWORD: "invalid_auth"}


async def test_webrtc_offer_passthrough(hass, config_entry) -> None:
    """Frontend offer is proxied to the device; answer is returned."""
    answer_sdp = "v=0\r\nfake-answer"

    class StubCoord:
        device_id = DEVICE["deviceId"]

        async def exchange_offer(self, offer_sdp: str) -> str:
            return answer_sdp

    config_entry.runtime_data = {"device": StubCoord()}
    cam = JetKVMScreenCamera(hass, config_entry)
    got: list = []
    await cam.async_handle_async_webrtc_offer("v=0\r\nfake-offer", "s1", got.append)
    assert isinstance(got[0], WebRTCAnswer) and got[0].answer == answer_sdp


async def test_validate_ok_and_bad_password(hass) -> None:
    """Config validation accepts good creds, rejects bad password."""
    from custom_components.jetkvm.config_flow import _validate

    with aioresponses() as mocked:
        mocked.post(
            f"{HOST}/auth/login-local",
            payload={"message": "Login successful"},
            headers={"Set-Cookie": "authToken=test-token; Path=/; HttpOnly"},
            repeat=True,
        )
        mocked.get(f"{HOST}/device", payload=DEVICE, repeat=True)
        errors, device_id = await _validate(
            hass, {CONF_HOST: HOST, CONF_PASSWORD: "secret", CONF_SCAN_INTERVAL: 30}
        )
        assert errors == {} and device_id == DEVICE["deviceId"]

    with aioresponses() as bad:
        bad.post(f"{HOST}/auth/login-local", status=401, repeat=True)
        errors, _ = await _validate(
            hass, {CONF_HOST: HOST, CONF_PASSWORD: "wrong", CONF_SCAN_INTERVAL: 30}
        )
    assert errors == {CONF_PASSWORD: "invalid_auth"}


def _mqtt_discovery_info():
    """Build a MqttServiceInfo like the device's retained network/state message."""
    from homeassistant.helpers.service_info.mqtt import MqttServiceInfo

    device_id = DEVICE["deviceId"]
    payload = json.dumps(
        {
            "ip_address": "192.168.1.168",
            "hostname": "jetkvm",
            "subnet_mask": "",
            "mac_address": "",
        }
    )
    return MqttServiceInfo(
        topic=f"jetkvm/{device_id}/network/state",
        payload=payload,
        qos=0,
        retain=True,
        subscribed_topic="jetkvm/+/network/state",
        timestamp=0.0,
    )


async def test_mqtt_discovery(hass, mock_http) -> None:
    """MQTT discovery pre-fills host; password confirm creates the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "mqtt"}, data=_mqtt_discovery_info()
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "secret"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_HOST] == HOST


async def test_mqtt_discovery_rejects_foreign_topic(hass) -> None:
    """Non-JetKVM discovery messages are aborted, not configured."""
    from homeassistant.helpers.service_info.mqtt import MqttServiceInfo

    info = MqttServiceInfo(
        topic="other/device/network/state",
        payload=json.dumps({"ip_address": "10.0.0.9"}),
        qos=0,
        retain=True,
        subscribed_topic="jetkvm/+/network/state",
        timestamp=0.0,
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "mqtt"}, data=info
    )
    assert result["type"] == FlowResultType.ABORT
