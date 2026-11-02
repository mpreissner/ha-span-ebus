"""Constants for the SPAN eBus integration."""

from __future__ import annotations

DOMAIN = "span_ebus"

# Config-entry data keys.
CONF_EMAIL = "email"
CONF_TOKENS = "tokens"
CONF_USER_ID = "user_id"
CONF_DEVICE_UUID = "device_uuid"
CONF_SERIAL = "serial"
# Key of the panel that owns the entry's identity. Set once, on the first start
# that can read the account; None means no panel does, so every panel is scoped.
CONF_PRIMARY_PANEL = "primary_panel"

# Config-entry option keys. `panels` absent means nothing has been chosen yet;
# `declined` is what the user has seen and left off, so it isn't announced again.
CONF_PANELS = "panels"
CONF_DECLINED = "declined"
CONF_FEEDS = "feeds"

# Where the auto-refreshing token file lives, relative to the HA config dir.
TOKEN_DIR = ".span_ebus"

MANUFACTURER = "SPAN"
MODEL = "Panel MAIN 40"
