"""What a Show may ask Home Assistant to do: the fixed table of verbs per domain (protocol §6), and nothing else.

A verb is the name of the Home Assistant service of the entity's own domain. **Only the verbs of this table exist**: there is no free-form
service call, no target, no template and no data key outside the verb's own list, so a Show that was taken over can ask for a listed verb on
a shared entity and for nothing else. Each verb's values are plain (numbers, booleans, short text, a colour) and range-checked.

Some verbs are *sensitive* (Principle 0, physical safety): unlocking, disarming, opening a garage door or a gate, opening a valve, turning a
camera or a siren off, and anything at all on a safety sensor's switch. They are never in the list the Show is given, unless the person
enabled them for that entity (and set a PIN), and then they need the PIN and a touch on the Show.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final

import voluptuous as vol

# ---------------------------------------------------------------------------------------------------------------------------------
# Plain values.

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _number(low: float, high: float, *, integer: bool = False) -> Callable[[Any], Any]:
    """A JSON number (not a flag) in a range."""

    def check(value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise vol.Invalid("a number is needed")
        if integer and isinstance(value, float):
            if not value.is_integer():
                raise vol.Invalid("a whole number is needed")
            value = int(value)
        if not low <= value <= high:
            raise vol.Invalid(f"a value from {low} to {high} is needed")
        return value

    return check


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise vol.Invalid("true or false is needed")
    return value


def _text(limit: int = 64) -> Callable[[Any], str]:
    """Short text with no control character."""

    def check(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > limit or _CONTROL.search(value):
            raise vol.Invalid(f"plain text of at most {limit} characters is needed")
        return value

    return check


def _rgb(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != 3:
        raise vol.Invalid("a colour is three numbers")
    return [_number(0, 255, integer=True)(v) for v in value]


def _args(**keys: Callable[[Any], Any]) -> vol.Schema:
    """The values a verb carries: these names and no others, each optional."""
    return vol.Schema({vol.Optional(name): check for name, check in keys.items()}, extra=vol.PREVENT_EXTRA)


NO_ARGS: Final = _args()

# ---------------------------------------------------------------------------------------------------------------------------------
# The table.

# `device_class`es of an entity whose every verb is sensitive: a safety sensor's switch is not an everyday thing to turn off.
SAFETY_CLASSES: Final = frozenset({"smoke", "carbon_monoxide", "gas", "moisture", "safety", "tamper", "problem"})
# `device_class`es of a cover that is a way into the house.
WAY_IN_CLASSES: Final = frozenset({"garage", "gate", "door"})
_WAY_IN_VERBS: Final = frozenset({"open_cover", "toggle", "set_cover_position", "open_cover_tilt"})

_SENSITIVE: Final = frozenset(
    {
        ("lock", "unlock"),
        ("lock", "open"),
        ("alarm_control_panel", "alarm_disarm"),
        ("valve", "open_valve"),
        ("valve", "set_valve_position"),
        ("valve", "toggle"),
        ("camera", "turn_off"),
        ("camera", "disable_motion_detection"),
        ("siren", "turn_on"),
        ("siren", "turn_off"),
        ("siren", "toggle"),
    }
)

_PERCENT = _number(0, 100)
_ON_OFF = {"turn_on": NO_ARGS, "turn_off": NO_ARGS, "toggle": NO_ARGS}

VERBS: Final[dict[str, dict[str, vol.Schema]]] = {
    "light": {
        "turn_on": _args(
            brightness_pct=_number(0, 100),
            brightness=_number(0, 255, integer=True),
            rgb_color=_rgb,
            color_temp_kelvin=_number(1000, 12000, integer=True),
            transition=_number(0, 300),
            effect=_text(64),
        ),
        "turn_off": _args(transition=_number(0, 300)),
        "toggle": NO_ARGS,
    },
    "switch": dict(_ON_OFF),
    "input_boolean": dict(_ON_OFF),
    "fan": {
        "turn_on": _args(percentage=_PERCENT, preset_mode=_text(32)),
        "turn_off": NO_ARGS,
        "toggle": NO_ARGS,
        "set_percentage": _args(percentage=_PERCENT),
        "set_preset_mode": _args(preset_mode=_text(32)),
        "oscillate": _args(oscillating=_flag),
        "increase_speed": NO_ARGS,
        "decrease_speed": NO_ARGS,
    },
    "cover": {
        "open_cover": NO_ARGS,
        "close_cover": NO_ARGS,
        "stop_cover": NO_ARGS,
        "toggle": NO_ARGS,
        "set_cover_position": _args(position=_PERCENT),
        "open_cover_tilt": NO_ARGS,
        "close_cover_tilt": NO_ARGS,
        "set_cover_tilt_position": _args(tilt_position=_PERCENT),
    },
    "climate": {
        "set_temperature": _args(
            temperature=_number(-50, 150),
            target_temp_high=_number(-50, 150),
            target_temp_low=_number(-50, 150),
            hvac_mode=_text(32),
        ),
        "set_hvac_mode": _args(hvac_mode=_text(32)),
        "set_preset_mode": _args(preset_mode=_text(32)),
        "set_fan_mode": _args(fan_mode=_text(32)),
        "turn_on": NO_ARGS,
        "turn_off": NO_ARGS,
    },
    "media_player": {
        "media_play": NO_ARGS,
        "media_pause": NO_ARGS,
        "media_play_pause": NO_ARGS,
        "media_stop": NO_ARGS,
        "media_next_track": NO_ARGS,
        "media_previous_track": NO_ARGS,
        "volume_set": _args(volume_level=_number(0, 1)),
        "volume_up": NO_ARGS,
        "volume_down": NO_ARGS,
        "volume_mute": _args(is_volume_muted=_flag),
        "turn_on": NO_ARGS,
        "turn_off": NO_ARGS,
        "select_source": _args(source=_text(64)),
    },
    "scene": {"turn_on": NO_ARGS},
    "script": {"turn_on": NO_ARGS},
    "automation": {"trigger": NO_ARGS},
    "button": {"press": NO_ARGS},
    "input_button": {"press": NO_ARGS},
    "number": {"set_value": _args(value=_number(-1e9, 1e9))},
    "input_number": {"set_value": _args(value=_number(-1e9, 1e9))},
    "select": {"select_option": _args(option=_text(64))},
    "input_select": {"select_option": _args(option=_text(64))},
    "humidifier": {
        "turn_on": NO_ARGS,
        "turn_off": NO_ARGS,
        "set_humidity": _args(humidity=_PERCENT),
        "set_mode": _args(mode=_text(32)),
    },
    "vacuum": {
        "start": NO_ARGS,
        "pause": NO_ARGS,
        "stop": NO_ARGS,
        "return_to_base": NO_ARGS,
        "locate": NO_ARGS,
    },
    "lock": {"lock": NO_ARGS, "unlock": NO_ARGS, "open": NO_ARGS},
    "alarm_control_panel": {
        "alarm_arm_home": NO_ARGS,
        "alarm_arm_away": NO_ARGS,
        "alarm_arm_night": NO_ARGS,
        "alarm_arm_vacation": NO_ARGS,
        "alarm_disarm": NO_ARGS,
    },
    "valve": {
        "close_valve": NO_ARGS,
        "open_valve": NO_ARGS,
        "set_valve_position": _args(position=_PERCENT),
        "toggle": NO_ARGS,
    },
    "camera": {
        "turn_on": NO_ARGS,
        "enable_motion_detection": NO_ARGS,
        "turn_off": NO_ARGS,
        "disable_motion_detection": NO_ARGS,
    },
    "siren": {"turn_on": NO_ARGS, "turn_off": NO_ARGS, "toggle": NO_ARGS},
}

# Domains that are only ever added to the list by naming them: a quick start (by area, device or type) never adds one.
NAMED_ONLY_DOMAINS: Final = frozenset({"script", "automation"})
# Domains whose every verb is sensitive: shared for their state, controlled only with the PIN.
SENSITIVE_ONLY_DOMAINS: Final = frozenset({"siren"})


def domain_of(entity_id: str) -> str:
    """The domain of an entity id (``light`` of ``light.kitchen``)."""
    return entity_id.partition(".")[0]


def is_sensitive(domain: str, verb: str, attributes: Mapping[str, Any] | None = None) -> bool:
    """Whether ``verb`` on an entity of ``domain`` (with these attributes) is a sensitive one."""
    device_class = (attributes or {}).get("device_class")
    if device_class in SAFETY_CLASSES:
        return True
    if (domain, verb) in _SENSITIVE:
        return True
    return domain == "cover" and device_class in WAY_IN_CLASSES and verb in _WAY_IN_VERBS


def verbs_of(domain: str) -> dict[str, vol.Schema]:
    """The verbs of a domain (none for a domain the Show may not act on)."""
    return VERBS.get(domain, {})


def verb_lists(
    entity_id: str,
    attributes: Mapping[str, Any] | None,
    *,
    control: bool,
    sensitive: bool,
) -> tuple[list[str], list[str]]:
    """The verbs the Show is told it may use on an entity now: ``(ctl, ctl_pin)``.

    ``control``: the person lets the Show act on this entity (its domain is allowed, it is not read-only, control is not paused).
    ``sensitive``: the person also enabled its sensitive verbs (and there is a PIN, and control is not paused).
    """
    domain = domain_of(entity_id)
    ordinary: list[str] = []
    needing_pin: list[str] = []
    for verb in verbs_of(domain):
        if is_sensitive(domain, verb, attributes):
            if sensitive:
                needing_pin.append(verb)
        elif control:
            ordinary.append(verb)
    return ordinary, needing_pin


class BadArgs(Exception):
    """The values of a verb are not what it takes."""


def validate_args(domain: str, verb: str, args: Any) -> dict[str, Any]:
    """The values of a call as the verb takes them, or ``BadArgs`` (an unknown name, a wrong type, a value out of range)."""
    schema = verbs_of(domain).get(verb)
    if schema is None:
        raise BadArgs("no such verb")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise BadArgs("the values are not an object")
    try:
        return dict(schema(args))
    except vol.Invalid as err:
        raise BadArgs(str(err)) from err


# ---------------------------------------------------------------------------------------------------------------------------------
# What a person reads in the logbook.

_PAST: Final = {
    "turn_on": "turned on",
    "turn_off": "turned off",
    "toggle": "toggled",
    "lock": "locked",
    "unlock": "unlocked",
    "open": "opened",
    "open_cover": "opened",
    "close_cover": "closed",
    "stop_cover": "stopped",
    "set_cover_position": "moved",
    "open_valve": "opened",
    "close_valve": "closed",
    "press": "pressed",
    "trigger": "triggered",
    "media_play": "played",
    "media_pause": "paused",
    "media_stop": "stopped",
    "media_next_track": "skipped to the next track on",
    "media_previous_track": "went back a track on",
    "alarm_arm_home": "armed (home)",
    "alarm_arm_away": "armed (away)",
    "alarm_arm_night": "armed (night)",
    "alarm_arm_vacation": "armed (vacation)",
    "alarm_disarm": "disarmed",
    "set_temperature": "set the temperature of",
    "set_hvac_mode": "set the mode of",
    "set_value": "set",
    "select_option": "chose an option on",
    "start": "started",
    "return_to_base": "sent home",
}


def describe(verb: str, name: str, args: Mapping[str, Any] | None = None) -> str:
    """``turned on Kitchen light`` and the like: what was done, for the logbook."""
    phrase = _PAST.get(verb) or verb.replace("_", " ")
    text = f"{phrase} {name}"
    if args:
        text += " (" + ", ".join(f"{k} {v}" for k, v in sorted(args.items())) + ")"
    return text
