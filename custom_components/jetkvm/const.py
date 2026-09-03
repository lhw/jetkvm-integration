"""Constants for the JetKVM integration."""

from __future__ import annotations

DOMAIN = "jetkvm"

CONF_HOST = "host"
CONF_PASSWORD = "password"  # noqa: S105 (config key name, not a credential)
CONF_SCAN_INTERVAL = "scan_interval"
CONF_WOL_MAC = "wol_mac"

DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
