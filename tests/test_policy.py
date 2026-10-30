"""The fixed table of verbs: what exists, what is sensitive, and what values are plain."""

from __future__ import annotations

import pytest

from custom_components.lumashow import policy
from custom_components.lumashow.attributes import filter_attributes
from custom_components.lumashow.policy import VERBS, BadArgs, describe, is_sensitive, validate_args, verb_lists


def test_every_verb_is_a_plain_service_name_and_there_is_no_free_form_call() -> None:
    for domain, verbs in VERBS.items():
        assert domain.isidentifier() and domain.islower()
        for verb in verbs:
            assert verb.isidentifier() and verb.islower(), (domain, verb)
    # What a Show must never be able to name: these are not domains of the table.
    for never in ("homeassistant", "shell_command", "rest_command", "notify", "persistent_notification", "hassio", "python_script", "mqtt"):
        assert never not in VERBS


def test_the_verbs_of_the_everyday_domains_are_the_ones_the_design_lists() -> None:
    assert set(VERBS["light"]) == {"turn_on", "turn_off", "toggle"}
    assert set(VERBS["switch"]) == {"turn_on", "turn_off", "toggle"}
    assert {"set_temperature", "set_hvac_mode"} <= set(VERBS["climate"])
    assert {"open_cover", "close_cover", "set_cover_position"} <= set(VERBS["cover"])
    assert {"media_play", "media_pause", "volume_set"} <= set(VERBS["media_player"])
    assert policy.verbs_of("vacuum_cleaner") == {}


@pytest.mark.parametrize(
    ("domain", "verb", "attrs", "sensitive"),
    [
        ("lock", "unlock", {}, True),
        ("lock", "open", {}, True),
        ("lock", "lock", {}, False),
        ("alarm_control_panel", "alarm_disarm", {}, True),
        ("alarm_control_panel", "alarm_arm_away", {}, False),
        ("cover", "open_cover", {"device_class": "garage"}, True),
        ("cover", "open_cover", {"device_class": "gate"}, True),
        ("cover", "open_cover", {"device_class": "door"}, True),
        ("cover", "set_cover_position", {"device_class": "garage"}, True),
        ("cover", "close_cover", {"device_class": "garage"}, False),
        ("cover", "stop_cover", {"device_class": "garage"}, False),
        ("cover", "open_cover", {"device_class": "blind"}, False),
        ("cover", "open_cover", {}, False),
        ("valve", "open_valve", {}, True),
        ("valve", "close_valve", {}, False),
        ("camera", "turn_off", {}, True),
        ("camera", "disable_motion_detection", {}, True),
        ("camera", "turn_on", {}, False),
        ("siren", "turn_on", {}, True),
        ("siren", "turn_off", {}, True),
        ("switch", "turn_off", {"device_class": "smoke"}, True),
        ("switch", "turn_on", {"device_class": "carbon_monoxide"}, True),
        ("switch", "turn_off", {"device_class": "outlet"}, False),
        ("light", "turn_on", {}, False),
    ],
)
def test_what_is_sensitive(domain: str, verb: str, attrs: dict[str, str], sensitive: bool) -> None:
    assert is_sensitive(domain, verb, attrs) is sensitive


def test_the_show_is_told_no_verb_unless_the_person_allowed_it() -> None:
    assert verb_lists("light.a", {}, control=False, sensitive=False) == ([], [])
    ordinary, needing_pin = verb_lists("light.a", {}, control=True, sensitive=False)
    assert ordinary == ["turn_on", "turn_off", "toggle"] and needing_pin == []
    # A lock with control allowed offers locking, and unlocking only when it was enabled for the entity.
    assert verb_lists("lock.door", {}, control=True, sensitive=False) == (["lock"], [])
    assert verb_lists("lock.door", {}, control=True, sensitive=True) == (["lock"], ["unlock", "open"])
    # A device's confirmation-only option offers a lock's unlock (not its open), with or without a PIN, and nothing on another domain.
    assert verb_lists("lock.door", {}, control=True, sensitive=False, confirm_unlock=True) == (["lock"], ["unlock"])
    assert verb_lists("lock.door", {}, control=True, sensitive=True, confirm_unlock=True) == (["lock"], ["unlock", "open"])
    assert verb_lists("cover.garage", {"device_class": "garage"}, control=True, sensitive=False, confirm_unlock=True)[1] == []
    assert verb_lists("siren.hall", {}, control=False, sensitive=False, confirm_unlock=True) == ([], [])
    # A siren has no ordinary verb at all.
    assert verb_lists("siren.hall", {}, control=True, sensitive=False) == ([], [])
    assert verb_lists("siren.hall", {}, control=False, sensitive=True)[1] == ["turn_on", "turn_off", "toggle"]
    # A garage door is closed freely and opened only with the PIN.
    garage = {"device_class": "garage"}
    ordinary, needing_pin = verb_lists("cover.garage", garage, control=True, sensitive=True)
    assert "close_cover" in ordinary and "open_cover" not in ordinary
    assert "open_cover" in needing_pin
    # A domain with no verbs gives nothing, whatever is allowed.
    assert verb_lists("sensor.t", {}, control=True, sensitive=True) == ([], [])


def test_values_are_plain_and_in_range() -> None:
    assert validate_args("light", "turn_on", {"brightness_pct": 40, "rgb_color": [1, 2, 3]}) == {
        "brightness_pct": 40,
        "rgb_color": [1, 2, 3],
    }
    assert validate_args("light", "turn_on", None) == {}
    assert validate_args("light", "turn_on", {"brightness": 128.0}) == {"brightness": 128}
    assert validate_args("media_player", "volume_set", {"volume_level": 0.5}) == {"volume_level": 0.5}
    assert validate_args("fan", "oscillate", {"oscillating": True}) == {"oscillating": True}


@pytest.mark.parametrize(
    ("domain", "verb", "args"),
    [
        ("light", "turn_on", {"brightness_pct": 101}),
        ("light", "turn_on", {"brightness_pct": -1}),
        ("light", "turn_on", {"brightness_pct": True}),
        ("light", "turn_on", {"brightness_pct": "40"}),
        ("light", "turn_on", {"brightness": 12.5}),
        ("light", "turn_on", {"rgb_color": [1, 2]}),
        ("light", "turn_on", {"rgb_color": [1, 2, 300]}),
        ("light", "turn_on", {"effect": "bad\nline"}),
        ("light", "turn_on", {"effect": "x" * 65}),
        ("light", "turn_on", {"effect": ""}),
        ("light", "turn_on", {"entity_id": "light.other"}),
        ("light", "turn_on", {"target": {"area_id": "all"}}),
        ("light", "turn_on", {"data_template": "{{ 1 }}"}),
        ("light", "toggle", {"brightness_pct": 5}),
        ("switch", "turn_on", {"anything": 1}),
        ("media_player", "volume_set", {"volume_level": 1.5}),
        ("fan", "oscillate", {"oscillating": "yes"}),
        ("light", "turn_on", [1, 2]),
        ("light", "fly", {}),
        ("homeassistant", "restart", {}),
    ],
)
def test_what_a_verb_does_not_take_is_refused(domain: str, verb: str, args: object) -> None:
    with pytest.raises(BadArgs):
        validate_args(domain, verb, args)


def test_a_text_with_a_template_is_plain_text_and_is_made_safe_before_it_is_written_down() -> None:
    # `effect` is plain text: the policy accepts a template as text, so the audit line must never render it (the hub strips braces).
    assert validate_args("light", "turn_on", {"effect": "{{ states('x') }}"}) == {"effect": "{{ states('x') }}"}
    assert describe("turn_on", "Kitchen light", {"effect": "rainbow"}) == "turned on Kitchen light (effect rainbow)"


def test_only_the_attributes_a_domain_lists_leave() -> None:
    light = {
        "friendly_name": "Kitchen",
        "brightness": 120,
        "supported_color_modes": ["xy", "hs"],
        "access_token": "secret",
        "entity_picture": "/api/x?token=y",
        "rgb_color": (1, 2, 3),
        "long": "x" * 500,
        "nested": {"a": 1},
        "icon": "x" * 129,
    }
    kept = filter_attributes("light", light)
    assert kept == {
        "friendly_name": "Kitchen",
        "brightness": 120,
        "supported_color_modes": ["xy", "hs"],
        "rgb_color": [1, 2, 3],
    }
    assert filter_attributes("camera", {"access_token": "t", "friendly_name": "Door", "entity_picture": "/x"}) == {"friendly_name": "Door"}
    assert filter_attributes("sensor", {"unit_of_measurement": "°C", "brightness": 4}) == {"unit_of_measurement": "°C"}
    assert filter_attributes("light", {"supported_color_modes": list(range(17))}) == {}
