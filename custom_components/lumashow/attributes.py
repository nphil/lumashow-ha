"""The attributes of an entity that leave Home Assistant (protocol §9): the ones its domain lists here, and no others.

A light's long ``supported_color_modes`` lists, a media player's queues, a camera's ``access_token`` and everything a custom integration
puts on its entities stay where they are. Only plain values go: a short text, a number, a flag, or a short list of those.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

MAX_TEXT: Final = 128
MAX_LIST: Final = 16
# A lamp's effects are its own names, and a floor lamp or a strip lists a hundred (the longest at the owner's has 111): that one list is cut at this
# many names, each at most as long as the `effect` verb takes (policy.py), where any other list over MAX_LIST is dropped.
MAX_EFFECTS: Final = 128
MAX_EFFECT_NAME: Final = 64

COMMON: Final = ("friendly_name", "device_class", "icon", "unit_of_measurement", "state_class")

BY_DOMAIN: Final[dict[str, tuple[str, ...]]] = {
    "light": (
        "brightness",
        "color_mode",
        "color_temp_kelvin",
        "min_color_temp_kelvin",
        "max_color_temp_kelvin",
        "hs_color",
        "rgb_color",
        "supported_color_modes",
        "effect",
        "effect_list",
    ),
    "event": ("event_type", "event_types"),
    "cover": ("current_position", "current_tilt_position"),
    "climate": (
        "current_temperature",
        "temperature",
        "target_temp_high",
        "target_temp_low",
        "target_temp_step",
        "min_temp",
        "max_temp",
        "current_humidity",
        "humidity",
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


def _effects(value: Any) -> tuple[bool, Any]:
    """The names of a lamp's effects that can be asked for again (text of at most MAX_EFFECT_NAME characters), the first MAX_EFFECTS of them."""
    if not isinstance(value, (list, tuple)):
        return False, None
    names = [v for v in value if isinstance(v, str) and 0 < len(v) <= MAX_EFFECT_NAME]
    return (True, names[:MAX_EFFECTS]) if names else (False, None)


def filter_attributes(domain: str, attributes: Mapping[str, Any]) -> dict[str, Any]:
    """The attributes of an entity of ``domain`` that may be sent."""
    allowed = COMMON + BY_DOMAIN.get(domain, ())
    out: dict[str, Any] = {}
    for name in allowed:
        if name in attributes:
            ok, value = _effects(attributes[name]) if name == "effect_list" else _clean(attributes[name])
            if ok:
                out[name] = value
    return out
