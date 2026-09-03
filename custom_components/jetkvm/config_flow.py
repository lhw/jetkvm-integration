"""Config flow for the JetKVM integration."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlowWithReload
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_WOL_MAC,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .coordinator import _auth_headers

LOGGER = logging.getLogger(__package__)
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _normalize_host(value: str | None) -> str | None:
    value = (value or "").strip().rstrip("/")
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    return value


def _is_valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): str,
            vol.Optional(CONF_WOL_MAC, default=defaults.get(CONF_WOL_MAC, "")): str,
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL)),
        }
    )


async def _validate(hass, data: dict[str, Any]) -> tuple[dict[str, str], str | None]:
    """Probe login + /device. Returns (errors, device_id)."""
    host = data.get(CONF_HOST)
    if not host or not _is_valid_url(host):
        return {CONF_HOST: "invalid"}, None
    base = host.rstrip("/")
    session = async_get_clientsession(hass)
    try:
        try:
            headers = await _auth_headers(session, base, data.get(CONF_PASSWORD, ""))
        except UpdateFailed as err:
            if "Invalid password" in str(err):
                return {CONF_PASSWORD: "invalid_auth"}, None
            return {CONF_HOST: "cannot_connect"}, None
        async with session.get(
            base + "/device", headers=headers, timeout=PROBE_TIMEOUT
        ) as resp:
            if resp.status >= 400:
                return {CONF_HOST: "cannot_connect"}, None
            payload = await resp.json()
    except (TimeoutError, aiohttp.ClientError):
        return {CONF_HOST: "cannot_connect"}, None
    except Exception:
        LOGGER.exception("Unexpected error probing %s", host)
        return {CONF_HOST: "unknown"}, None
    device_id = payload.get("deviceId") if isinstance(payload, dict) else None
    return {}, device_id or host


class JetKVMConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the JetKVM config flow."""

    def __init__(self) -> None:
        """Initialize (discovery state filled by async_step_mqtt)."""
        self._discovered: dict[str, str] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            data[CONF_HOST] = _normalize_host(data.get(CONF_HOST))
            errors, device_id = await _validate(self.hass, data)
            if not errors:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured(error="unique_already_configured")
                return self.async_create_entry(title=data[CONF_HOST], data=data)
        return self.async_show_form(
            step_id="user", data_schema=_schema({}), errors=errors
        )

    async def async_step_mqtt(self, discovery_info: MqttServiceInfo) -> FlowResult:
        """Handle JetKVM MQTT discovery via the device's own network/state topic.

        We subscribe to ``jetkvm/+/network/state`` (the device's own retained
        state topic, not a ``homeassistant/`` prefix) so this doesn't collide
        with standard MQTT device discovery. The device id comes from the topic
        and the host IP from the payload's ip_address field.
        """
        parts = discovery_info.topic.split("/")
        if len(parts) != 4 or parts[0] != "jetkvm" or parts[2:] != ["network", "state"]:
            return self.async_abort(reason="invalid_discovery_info")
        device_id = parts[1]
        if not device_id:
            return self.async_abort(reason="invalid_discovery_info")
        try:
            payload = json.loads(discovery_info.payload)
            ip_address = payload["ip_address"]
        except (ValueError, KeyError, TypeError, AttributeError):
            return self.async_abort(reason="invalid_discovery_info")
        if not ip_address:
            return self.async_abort(reason="invalid_discovery_info")
        host = f"http://{ip_address}"
        if not _is_valid_url(host):
            return self.async_abort(reason="invalid_discovery_info")
        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()
        self._discovered = {"host": host.rstrip("/"), "device_id": device_id}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a discovered device (password still required)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_HOST: self._discovered["host"],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_WOL_MAC: "",
                CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
            }
            errors, _ = await _validate(self.hass, data)
            if not errors:
                return self.async_create_entry(title=data[CONF_HOST], data=data)
        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"host": self._discovered.get("host", "")},
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> JetKVMOptionsFlow:
        return JetKVMOptionsFlow()


class JetKVMOptionsFlow(OptionsFlowWithReload):
    """Options: password + WOL MAC + interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = dict(user_input)
            data[CONF_HOST] = _normalize_host(
                data.get(CONF_HOST) or self.config_entry.data.get(CONF_HOST)
            )
            errors, _ = await _validate(self.hass, data)
            if not errors:
                return self.async_create_entry(data=data)
        entry = self.config_entry
        defaults = {
            CONF_HOST: entry.data.get(CONF_HOST, ""),
            CONF_PASSWORD: entry.data.get(CONF_PASSWORD, ""),
            CONF_WOL_MAC: (entry.options or entry.data).get(CONF_WOL_MAC, ""),
            CONF_SCAN_INTERVAL: (entry.options or entry.data).get(
                CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
            ),
        }
        return self.async_show_form(
            step_id="init", data_schema=_schema(defaults), errors=errors
        )
