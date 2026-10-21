"""The attributes of an entity that leave Home Assistant (protocol §9): the ones its domain lists here, and no others.

A light's long ``supported_color_modes`` lists, a media player's queues, a camera's ``access_token`` and everything a custom integration
puts on its entities stay where they are. Only plain values go: a short text, a number, a flag, or a short list of those.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

MAX_TEXT: Final = 128
MAX_LIST: Final = 16

COMMON: Final = ("friendly_name", "device_class", "icon", "unit_of_measurement", "state_class")

BY_DOMAIN: Final[dict[str, tuple[str, ...]]] = {
    "light": (
        "brightness",
        "color_mode",
        "color_temp_kelvin",
        "rgb_color",
        "supported_color_modes",
        "effect",
    ),
    "event": ("event_type", "event_types"),
    "cover": ("current_position", "current_tilt_position"),
    "climate": (
        "current_temperature",
        "temperature",
        "target_temp_high",
        "target_temp_low",
        "hvac_action",
        "hvac_modes",
        "preset_mode",
    ),
    "media_player": ("media_title", "media_artist", "volume_level", "is_volume_muted", "source"),
    "fan": ("percentage", "preset_mode", "oscillating"),
}


def _plain(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return value is not None
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and len(value) <= MAX_TEXT


def _clean(value: Any) -> tuple[bool, Any]:
    """Whether the value may go, and as what (a tuple becomes a list)."""
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST or not all(_plain(v) for v in value):
            return False, None
        return True, list(value)
    return (True, value) if _plain(value) else (False, None)


def filter_attributes(domain: str, attributes: Mapping[str, Any]) -> dict[str, Any]:
    """The attributes of an entity of ``domain`` that may be sent."""
    allowed = COMMON + BY_DOMAIN.get(domain, ())
    out: dict[str, Any] = {}
    for name in allowed:
        if name in attributes:
            ok, value = _clean(attributes[name])
            if ok:
                out[name] = value
    return out
