"""Constants of the LumaShow integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "lumashow"
# The version this integration reports in its hello (the manifest's: a test holds them together).
VERSION: Final = "0.1.0"

NAME: Final = "LumaShow"

# The Show listens here, and announces itself as this multicast DNS service.
DEFAULT_PORT: Final = 6054
SERVICE_TYPE: Final = "_lumashow._tcp.local."

# Config entry data (set by the config flow; never changed by the options).
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_MAC: Final = "mac"
CONF_KEY: Final = "key"  # the Show's pairing key, base64 (the ESPHome device's own)
CONF_KEY_SOURCE: Final = "key_source"  # "esphome" (found in its config entry) or "manual"
CONF_ESPHOME_ENTRY: Final = "esphome_entry_id"
CONF_FRIENDLY_NAME: Final = "friendly_name"

KEY_SOURCE_ESPHOME: Final = "esphome"
KEY_SOURCE_MANUAL: Final = "manual"

# Options (per Show). Nothing is shared until the person adds something.
OPT_ENTITIES: Final = "entities"  # the list: what the Show is given, exactly
OPT_CONTROL_DOMAINS: Final = "control_domains"  # domains whose ordinary verbs the Show may use
OPT_READ_ONLY: Final = "read_only"  # shared entities the Show may never act on
OPT_SENSITIVE: Final = "sensitive"  # entities whose sensitive verbs are enabled (with the PIN)
OPT_PIN_HASH: Final = "pin_hash"
OPT_PIN_SALT: Final = "pin_salt"
OPT_FORWARD_NOTIFICATIONS: Final = "forward_notifications"  # Home Assistant's persistent notifications
OPT_SHARE_STREAMS: Final = "share_streams"  # camera addresses (may carry a password)
OPT_SHARE_ALARMS: Final = "share_alarms"  # the alarm clocks: the Show gets and writes the alarms calendar
OPT_CAMERA_EVENTS: Final = "camera_events"  # what the shared cameras' detectors see (person, vehicle, animal, doorbell), as it happens

# Rules (protocol §11).
RATE_CALLS: Final = 10
RATE_WINDOW: Final = 10.0  # seconds
PIN_MIN_LEN: Final = 4
PIN_MAX_LEN: Final = 8
PIN_FAILURES: Final = 5  # wrong PINs that lock the sensitive verbs ...
PIN_WINDOW: Final = 600.0  # ... within this many seconds ...
PIN_LOCKOUT: Final = 600.0  # ... for this long
MAX_ENTITIES: Final = 256
MAX_CALL_ENTITIES: Final = 16

# Timing of the channel.
PING_EVERY: Final = 25.0
SILENT_AFTER: Final = 75.0
CONNECT_TIMEOUT: Final = 10.0
BACKOFF_MIN: Final = 1.0
BACKOFF_MAX: Final = 60.0
PUSH_DEBOUNCE: Final = 0.25
LAST_PUSH_EVERY: Final = 60.0
CALL_TIMEOUT: Final = 10.0
SNAPSHOT_URL_LIFETIME: Final = 3600  # seconds a signed picture address is good for
SNAPSHOT_URL_REFRESH: Final = 1800

# Events and signals.
EVENT_ACTION: Final = "lumashow_action"
SIGNAL_UPDATE: Final = f"{DOMAIN}_update_{{}}"  # formatted with the entry id
