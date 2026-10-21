"""Diagnostics: what a person can send when something does not work. The key and the PIN are never in it."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import LumaShowConfigEntry
from .const import CONF_KEY, OPT_PIN_HASH, OPT_PIN_SALT

TO_REDACT = {CONF_KEY, OPT_PIN_HASH, OPT_PIN_SALT, "noise_psk"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: LumaShowConfigEntry) -> dict[str, Any]:
    hub = entry.runtime_data
    return {
        "entry": async_redact_data({"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT),
        "hub": {
            "connected": hub.connected,
            "agreed_modules": hub.agreed,
            "device": hub.device,
            "rev": hub.rev,
            "shared": hub.shared_count,
            "controllable": hub.controllable_count,
            "paused": hub.paused,
            "actions": hub.actions,
            "refused": hub.refused,
            "last_action": hub.last_action,
            "last_push": hub.last_push.isoformat() if hub.last_push else None,
        },
    }
