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

# Config-entry data keys for a panel added over its local API. The host is
# HA's `host`. An entry with these and no `tokens` runs without the cloud.
CONF_LOCAL_SERIAL = "local_serial"
CONF_CA_PEM = "ca_pem"
CONF_CLIENT_NAME = "client_name"
CONF_ACCESS_TOKEN = "access_token"
CONF_BROKER_USERNAME = "broker_username"
CONF_BROKER_PASSWORD = "broker_password"
CONF_MQTTS_PORT = "mqtts_port"

# Config-entry option keys. `panels` absent means nothing has been chosen yet;
# `declined` is what the user has seen and left off, so it isn't announced again.
CONF_PANELS = "panels"
CONF_DECLINED = "declined"
CONF_FEEDS = "feeds"
# A local panel's transport: `local_first` (the default) fails over to the
# cloud when the entry has cloud credentials; `local_only` never does.
CONF_TRANSPORT_MODE = "transport_mode"

# Marks a reauth flow as the local broker's, in the flow's entry data: a linked
# entry also has cloud tokens, which would otherwise route it to the cloud form.
LOCAL_REAUTH = "local_reauth"

# Where the auto-refreshing token file lives, relative to the HA config dir.
TOKEN_DIR = ".span_ebus"

MANUFACTURER = "SPAN"
MODEL = "Panel MAIN 40"
