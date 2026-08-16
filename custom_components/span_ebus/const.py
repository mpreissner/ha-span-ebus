"""Constants for the SPAN eBus integration."""

from __future__ import annotations

DOMAIN = "span_ebus"

# Config-entry data keys.
CONF_EMAIL = "email"
CONF_TOKENS = "tokens"
CONF_USER_ID = "user_id"
CONF_DEVICE_UUID = "device_uuid"
CONF_SERIAL = "serial"

# Where the auto-refreshing token file lives, relative to the HA config dir.
TOKEN_DIR = ".span_ebus"

MANUFACTURER = "SPAN"
MODEL = "Panel MAIN 40"
