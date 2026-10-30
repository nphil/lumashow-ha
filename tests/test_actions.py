"""What the Show may ask, and what is done about it: the integration decides, every time, and writes it down."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.lumashow.const import (
    OPT_READ_ONLY,
    OPT_SENSITIVE,
    OPT_UNLOCK_CONFIRM_ONLY,
)
from custom_components.lumashow.hub import module_handlers

from .fake_show import FakeShow
from .rig import PIN, Rig, ask, light, with_pin

# -- what is done ---------------------------------------------------------------------------------------------------------------


async def test_an_allowed_action_is_done_answered_counted_and_in_the_logbook(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await rig.start(["light.a"], ["light"])
    result = await ask(rig.session, ["light.a"], "turn_on", {"brightness_pct": 40}, origin="plugin")
    assert result["ok"] is True and result["results"] == [{"entity_id": "light.a", "ok": True}]
    assert [c.data for c in calls] == [{"entity_id": "light.a", "brightness_pct": 40}]
    # The audit: `<Show>: <what>` against the entity, an event, a counter.
    assert len(rig.logbook) == 1
    entry = rig.logbook[0].data
    assert entry["name"] == "Living Room Show:" and entry["message"] == "turned on Kitchen light (brightness_pct 40)"
    assert entry["entity_id"] == "light.a" and entry["domain"] == "lumashow"
    assert rig.events[0].data == {
        "device": "Living Room Show",
        "entity_id": "light.a",
        "verb": "light.turn_on",
        "origin": "plugin",
        "ok": True,
        "code": None,
    }
    assert rig.hub.actions == 1 and rig.hub.refused == 0
    await hass.async_block_till_done()
    assert hass.states.get("sensor.living_room_show_actions").state == "1"
    assert hass.states.get("sensor.living_room_show_actions").attributes["last_action"].startswith("turned on Kitchen light")


async def test_a_call_for_several_entities_is_done_for_each_and_written_down_for_each(hass: HomeAssistant, rig: Rig) -> None:
    light(hass, "light.a", "A")
    light(hass, "light.b", "B")
    calls = async_mock_service(hass, "light", "turn_off")
    await rig.start(["light.a", "light.b"], ["light"])
    result = await ask(rig.session, ["light.a", "light.b"], "turn_off")
    assert result["ok"] is True and {r["entity_id"] for r in result["results"]} == {"light.a", "light.b"}
    assert {c.data["entity_id"] for c in calls} == {"light.a", "light.b"}
    assert [e.data["entity_id"] for e in rig.logbook] == ["light.a", "light.b"] or len(rig.logbook) == 2
    assert rig.hub.actions == 2


async def test_a_service_that_fails_is_a_failed_result_and_is_written_down(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)

    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("the lamp is unplugged")

    hass.services.async_register("light", "turn_on", broken)
    await rig.start(["light.a"], ["light"])
    result = await ask(rig.session, ["light.a"], "turn_on")
    assert result["ok"] is False and result["code"] == "failed"
    assert rig.logbook[0].data["message"].startswith("tried to turned on Kitchen light") or "failed" in rig.logbook[0].data["message"]
    assert rig.events[0].data["ok"] is False and rig.events[0].data["code"] == "failed"


async def test_the_show_cannot_make_the_logbook_render_a_template(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    async_mock_service(hass, "light", "turn_on")
    await rig.start(["light.a"], ["light"])
    await ask(rig.session, ["light.a"], "turn_on", {"effect": "{{ states('lock.front_door') }}"})
    message = rig.logbook[0].data["message"]
    assert "{" not in message and "}" not in message


# -- what is refused -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "entity_ids", "verb", "args", "code"),
    [
        ("a verb that is not in the table", ["light.a"], "fly", None, "unknown_verb"),
        ("a domain that is not in the table", ["homeassistant.restart"], "turn_on", None, "unknown_verb"),
        ("a shell command", ["shell_command.rm"], "turn_on", None, "unknown_verb"),
        ("a notify service", ["notify.everyone"], "send_message", {"message": "x"}, "unknown_verb"),
        ("an entity that is not shared", ["light.private"], "turn_on", None, "not_allowed"),
        ("a domain the person did not allow", ["switch.b"], "turn_on", None, "not_allowed"),
        ("an entity held read-only", ["light.readonly"], "turn_on", None, "not_allowed"),
        ("a script that was not picked", ["script.goodnight"], "turn_on", None, "not_allowed"),
        ("a target smuggled in as a value", ["light.a"], "turn_on", {"entity_id": "all"}, "bad_args"),
        ("an area as a value", ["light.a"], "turn_on", {"area_id": "kitchen"}, "bad_args"),
        ("a template as a key", ["light.a"], "turn_on", {"data_template": "x"}, "bad_args"),
        ("a value out of range", ["light.a"], "turn_on", {"brightness_pct": 400}, "bad_args"),
        ("a value of the wrong kind", ["light.a"], "turn_on", {"brightness_pct": True}, "bad_args"),
        ("values on a verb that takes none", ["light.a"], "toggle", {"brightness_pct": 5}, "bad_args"),
        ("values that are not an object", ["light.a"], "turn_on", [1, 2], "bad_args"),
        ("entities of two domains", ["light.a", "switch.b"], "turn_on", None, "bad_args"),
        ("no entities", [], "turn_on", None, "bad_args"),
        ("entities that are one text", "light.a", "turn_on", None, "bad_args"),
        ("a verb that is no text", ["light.a"], 7, None, "unknown_verb"),
        ("too many entities", [f"light.l{i}" for i in range(17)], "turn_on", None, "too_many"),
    ],
)
async def test_what_the_table_and_the_person_do_not_allow_is_refused_and_nothing_is_done(
    hass: HomeAssistant, rig: Rig, name: str, entity_ids: Any, verb: Any, args: Any, code: str
) -> None:
    light(hass)
    light(hass, "light.readonly")
    hass.states.async_set("switch.b", "off")
    hass.states.async_set("script.goodnight", "off")
    hass.states.async_set("light.private", "on")
    done = {
        domain: async_mock_service(hass, domain, service)
        for domain, service in [
            ("light", "turn_on"),
            ("light", "toggle"),
            ("switch", "turn_on"),
            ("script", "turn_on"),
            ("homeassistant", "restart"),
            ("shell_command", "rm"),
            ("notify", "send_message"),
        ]
    }
    await rig.start(["light.a", "light.readonly", "switch.b"], ["light"], **{OPT_READ_ONLY: ["light.readonly"]})
    result = await ask(rig.session, entity_ids, verb, args)
    assert result["ok"] is False and result["code"] == code, name
    assert all(not calls for calls in done.values()), f"{name}: a service was called"
    assert rig.hub.actions == 0 and rig.hub.refused == 1, name
    assert rig.events[0].data["ok"] is False and rig.events[0].data["code"] == code


async def test_nothing_beyond_the_verb_is_ever_called_whatever_else_the_message_says(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    lights = async_mock_service(hass, "light", "turn_on")
    other = async_mock_service(hass, "homeassistant", "restart")
    await rig.start(["light.a"], ["light"])
    result = await ask(
        rig.session,
        ["light.a"],
        "turn_on",
        service="homeassistant.restart",
        domain="homeassistant",
        target={"area_id": "all"},
        data={"entity_id": "all"},
    )
    assert result["ok"] is True
    assert len(lights) == 1 and lights[0].data == {"entity_id": "light.a"} and not other


async def test_a_message_without_an_operation_number_is_answered_and_refused_not_fatal(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    await rig.start(["light.a"], ["light"])
    await rig.session.send({"m": "actions", "t": "call", "entity_ids": ["light.a"], "verb": "fly"})
    result = await rig.session.wait(lambda m: m["m"] == "actions" and m["t"] == "result" and m["op"] == 0)
    assert result["code"] == "unknown_verb"
    assert rig.hub.connected


async def test_a_flood_of_refusals_is_counted_all_but_logged_in_the_logbook_once_in_ten_seconds(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    await rig.start(["light.a"], ["light"])
    clock = [1000.0]
    rig.hub.clock = lambda: clock[0]
    for _ in range(5):
        await ask(rig.session, ["light.a"], "fly")
    assert rig.hub.refused == 5 and len(rig.logbook) == 1
    clock[0] += 11.0
    await ask(rig.session, ["light.a"], "fly")
    assert len(rig.logbook) == 2


# -- the rate and the brake ----------------------------------------------------------------------------------------------------


async def test_ten_calls_in_ten_seconds_and_no_more(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    calls = async_mock_service(hass, "light", "toggle")
    await rig.start(["light.a"], ["light"])
    clock = [1000.0]
    rig.hub.clock = lambda: clock[0]
    codes = [(await ask(rig.session, ["light.a"], "toggle")).get("code") for _ in range(11)]
    assert codes[:10] == [None] * 10 and codes[10] == "rate_limited"
    assert len(calls) == 10
    clock[0] += 10.5
    assert (await ask(rig.session, ["light.a"], "toggle"))["ok"] is True


async def test_garbage_is_rate_limited_too(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    await rig.start(["light.a"], ["light"])
    codes = [(await ask(rig.session, ["light.a"], "fly")).get("code") for _ in range(12)]
    assert codes[:10] == ["unknown_verb"] * 10 and codes[10:] == ["rate_limited"] * 2


async def test_pausing_control_tells_the_show_it_may_do_nothing_and_refuses_what_it_asks(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    await rig.start(["light.a"], ["light"])
    first = await rig.session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot")
    assert first["entities"][0]["ctl"] and first["policy"]["paused"] is False
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.living_room_show_pause_control"}, blocking=True)
    paused = await rig.session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot" and m["policy"]["paused"])
    assert "ctl" not in paused["entities"][0]
    result = await ask(rig.session, ["light.a"], "turn_on")
    assert result["code"] == "paused" and not calls
    assert hass.states.get("switch.living_room_show_pause_control").state == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.living_room_show_pause_control"}, blocking=True)
    assert (await ask(rig.session, ["light.a"], "turn_on"))["ok"] is True and len(calls) == 1


async def test_the_pause_is_remembered_across_a_restart(
    hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    rig = await Rig(hass, show, entry).start(["sensor.x"])
    rig.hub.set_paused(True)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass_storage[f"lumashow.{entry.entry_id}"]["data"]["paused"] is True
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.paused is True


# -- the sensitive verbs -------------------------------------------------------------------------------------------------------


async def sensitive_rig(hass: HomeAssistant, rig: Rig, **extra: Any) -> list[ServiceCall]:
    hass.states.async_set("lock.door", "locked", {"friendly_name": "Front door"})
    async_mock_service(hass, "lock", "lock")
    unlocks = async_mock_service(hass, "lock", "unlock")
    await rig.start(["lock.door"], ["lock"], **with_pin(**{OPT_SENSITIVE: ["lock.door"], **extra}))
    return unlocks


async def test_locking_needs_no_pin_and_unlocking_is_not_offered_unless_it_was_enabled(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("lock.door", "locked")
    locks = async_mock_service(hass, "lock", "lock")
    unlocks = async_mock_service(hass, "lock", "unlock")
    await rig.start(["lock.door"], ["lock"], **with_pin())
    snapshot = await rig.session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot")
    assert snapshot["entities"][0]["ctl"] == ["lock"] and "ctl_pin" not in snapshot["entities"][0]
    assert (await ask(rig.session, ["lock.door"], "lock"))["ok"] is True and len(locks) == 1
    refused = await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin=PIN)
    assert refused["code"] == "sensitive_disabled" and not unlocks


async def test_an_enabled_sensitive_verb_needs_a_touch_a_confirmation_and_the_pin(hass: HomeAssistant, rig: Rig) -> None:
    unlocks = await sensitive_rig(hass, rig)
    snapshot = await rig.session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot")
    assert snapshot["entities"][0]["ctl_pin"] == ["unlock", "open"] and snapshot["policy"]["pin_set"] is True
    # Never from a plugin, voice or an automation, whatever else is in the message.
    for origin in ("plugin", "voice", "automation", "nonsense"):
        result = await ask(rig.session, ["lock.door"], "unlock", origin=origin, confirmed=True, pin=PIN)
        assert result["code"] == "needs_touch", origin
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", pin=PIN))["code"] == "needs_confirm"
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed="yes", pin=PIN))["code"] == "needs_confirm"
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True))["code"] == "pin_required"
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin=""))["code"] == "pin_required"
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin="0000"))["code"] == "pin_wrong"
    assert not unlocks
    good = await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin=PIN)
    assert good["ok"] is True and [c.data for c in unlocks] == [{"entity_id": "lock.door"}]
    assert rig.logbook[-1].data["message"] == "unlocked Front door"
    assert PIN not in str(rig.logbook) and PIN not in str([e.data for e in rig.events])


async def test_five_wrong_pins_lock_the_sensitive_verbs_for_ten_minutes_even_for_the_right_pin(hass: HomeAssistant, rig: Rig) -> None:
    unlocks = await sensitive_rig(hass, rig)
    clock = [5000.0]
    rig.hub.clock = lambda: clock[0]
    for attempt in range(4):
        clock[0] += 12  # not the rate limit's business
        assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin="1111"))["code"] == "pin_wrong", attempt
    clock[0] += 12
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin="1111"))["code"] == "locked_out"
    clock[0] += 12
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin=PIN))["code"] == "locked_out"
    assert not unlocks
    # Everyday verbs are not locked out with them.
    assert (await ask(rig.session, ["lock.door"], "lock"))["ok"] is True
    clock[0] += 601
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin=PIN))["ok"] is True
    assert len(unlocks) == 1


async def test_a_garage_door_closes_freely_and_opens_only_with_the_pin_when_it_was_enabled(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("cover.garage", "closed", {"device_class": "garage", "friendly_name": "Garage"})
    opens = async_mock_service(hass, "cover", "open_cover")
    closes = async_mock_service(hass, "cover", "close_cover")
    await rig.start(["cover.garage"], ["cover"], **with_pin(**{OPT_SENSITIVE: ["cover.garage"]}))
    assert (await ask(rig.session, ["cover.garage"], "close_cover"))["ok"] is True and len(closes) == 1
    assert (await ask(rig.session, ["cover.garage"], "open_cover"))["code"] == "needs_touch"
    assert not opens
    assert (await ask(rig.session, ["cover.garage"], "open_cover", origin="touch", confirmed=True, pin=PIN))["ok"] is True
    assert len(opens) == 1


async def test_a_sensitive_verb_cannot_be_enabled_without_a_pin_and_a_removed_pin_takes_them_with_it(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("lock.door", "locked")
    unlocks = async_mock_service(hass, "lock", "unlock")
    await rig.start(["lock.door"], ["lock"], **{OPT_SENSITIVE: ["lock.door"]})  # enabled in the options, but no PIN exists
    snapshot = await rig.session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot")
    assert "ctl_pin" not in snapshot["entities"][0]
    result = await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True, pin=PIN)
    assert result["code"] == "sensitive_disabled" and not unlocks


async def test_a_siren_has_no_everyday_verb_and_a_safety_sensors_switch_is_sensitive(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("siren.hall", "off")
    hass.states.async_set("switch.smoke_alarm", "on", {"device_class": "smoke"})
    await rig.start(["siren.hall", "switch.smoke_alarm"], ["siren", "switch"], **with_pin())
    assert (await ask(rig.session, ["siren.hall"], "turn_on"))["code"] == "sensitive_disabled"
    assert (await ask(rig.session, ["switch.smoke_alarm"], "turn_off"))["code"] == "sensitive_disabled"


# -- the integration's own hook for modules ------------------------------------------------------------------------------------


class Recorder:
    version = 1

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def async_session(self, hub: Any, up: bool) -> None:
        self.events.append(("session", up))

    async def async_message(self, hub: Any, kind: str, body: Any) -> None:
        self.events.append((kind, dict(body)))


async def test_a_module_that_someone_registered_is_offered_and_carried_and_the_others_are_ignored(hass: HomeAssistant, rig: Rig) -> None:
    recorder = Recorder()
    module_handlers(hass)["alarms"] = recorder
    rig.show.modules["alarms"] = 1
    await rig.start([])
    hello = await rig.session.wait_kind("core", "hello")
    assert hello["modules"]["alarms"] == 1
    await asyncio.sleep(0.1)
    await rig.session.send({"m": "alarms", "t": "put", "uid": "ls-1"})
    await rig.session.send({"m": "media", "t": "now"})
    await rig.session.send({"m": "states", "t": "from_the_future"})
    await asyncio.sleep(0.3)
    assert ("session", True) in recorder.events and ("put", {"uid": "ls-1"}) in recorder.events
    assert not [e for e in recorder.events if e[0] in ("now", "from_the_future")]
    assert rig.hub.connected


# -- unlocking from this device with a confirmation only -----------------------------------------------------------------------


async def confirm_only_rig(hass: HomeAssistant, rig: Rig, *, pin: bool, **extra: Any) -> list[ServiceCall]:
    hass.states.async_set("lock.door", "locked", {"friendly_name": "Front door"})
    hass.states.async_set("lock.back", "locked", {"friendly_name": "Back door"})
    async_mock_service(hass, "lock", "lock")
    unlocks = async_mock_service(hass, "lock", "unlock")
    opens = async_mock_service(hass, "lock", "open")
    options = {OPT_SENSITIVE: ["lock.door"], OPT_UNLOCK_CONFIRM_ONLY: True, **extra}
    await rig.start(["lock.door", "lock.back"], ["lock"], **(with_pin(**options) if pin else options))
    assert opens == []
    return unlocks


@pytest.mark.parametrize("pin", [False, True])
async def test_with_the_option_on_a_confirmed_touch_unlocks_a_lock_without_the_pin(hass: HomeAssistant, rig: Rig, pin: bool) -> None:
    unlocks = await confirm_only_rig(hass, rig, pin=pin)
    snapshot = await rig.session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot")
    by_id = {e["id"]: e for e in snapshot["entities"]}
    assert snapshot["policy"]["pin_set"] is pin
    # offered for the lock the person allowed, only its unlock without a PIN (the latch opens only with one), nothing for the other lock
    assert by_id["lock.door"]["ctl_pin"] == (["unlock", "open"] if pin else ["unlock"]) and "ctl_pin" not in by_id["lock.back"]
    result = await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True)
    assert result["ok"] is True and [c.data for c in unlocks] == [{"entity_id": "lock.door"}]
    assert rig.logbook[-1].data["message"] == "unlocked Front door"
    assert rig.events[-1].data["verb"] == "lock.unlock" and rig.events[-1].data["origin"] == "touch" and rig.events[-1].data["ok"] is True


async def test_with_the_option_on_every_other_way_to_unlock_is_still_refused(hass: HomeAssistant, rig: Rig) -> None:
    unlocks = await confirm_only_rig(hass, rig, pin=False)
    clock = [5000.0]
    rig.hub.clock = lambda: clock.__setitem__(0, clock[0] + 3) or clock[0]  # more calls than the rate limit's ten in ten seconds
    # not from a plugin, a voice, an automation, nor a request with no origin, even with the confirmation in it
    for origin in ("plugin", "voice", "automation", "nonsense", None):
        extra = {} if origin is None else {"origin": origin}
        assert (await ask(rig.session, ["lock.door"], "unlock", confirmed=True, **extra))["code"] == "needs_touch", origin
    # not without the confirmation, nor with anything but the boolean true
    for confirmed in (None, False, "yes", 1, "true"):
        extra = {} if confirmed is None else {"confirmed": confirmed}
        assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", **extra))["code"] == "needs_confirm", confirmed
    # the latch is not the lock's unlock: it still needs the PIN, and there is none
    assert (await ask(rig.session, ["lock.door"], "open", origin="touch", confirmed=True))["code"] == "sensitive_disabled"
    # a lock the person did not allow under Sensitive actions
    assert (await ask(rig.session, ["lock.back"], "unlock", origin="touch", confirmed=True))["code"] == "sensitive_disabled"
    assert not unlocks and rig.hub.actions == 0


async def test_with_the_option_on_and_a_pin_the_latch_still_needs_the_pin(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("lock.door", "locked")
    opens = async_mock_service(hass, "lock", "open")
    await rig.start(["lock.door"], ["lock"], **with_pin(**{OPT_SENSITIVE: ["lock.door"], OPT_UNLOCK_CONFIRM_ONLY: True}))
    assert (await ask(rig.session, ["lock.door"], "open", origin="touch", confirmed=True))["code"] == "pin_required"
    assert (await ask(rig.session, ["lock.door"], "open", origin="touch", confirmed=True, pin="0000"))["code"] == "pin_wrong"
    assert not opens
    assert (await ask(rig.session, ["lock.door"], "open", origin="touch", confirmed=True, pin=PIN))["ok"] is True and len(opens) == 1


async def test_with_the_option_off_a_confirmed_touch_alone_never_unlocks(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("lock.door", "locked")
    unlocks = async_mock_service(hass, "lock", "unlock")
    await rig.start(["lock.door"], ["lock"], **with_pin(**{OPT_SENSITIVE: ["lock.door"]}))
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True))["code"] == "pin_required"
    assert not unlocks


async def test_a_call_for_several_locks_is_done_only_when_the_person_allowed_every_one(hass: HomeAssistant, rig: Rig) -> None:
    unlocks = await confirm_only_rig(hass, rig, pin=False)
    assert (await ask(rig.session, ["lock.door", "lock.back"], "unlock", origin="touch", confirmed=True))["code"] == "sensitive_disabled"
    assert not unlocks
    rig.hass.config_entries.async_update_entry(rig.entry, options={**rig.entry.options, OPT_SENSITIVE: ["lock.door", "lock.back"]})
    await rig.hass.async_block_till_done()
    rig.session = await rig.show.wait_session()
    result = await ask(rig.session, ["lock.door", "lock.back"], "unlock", origin="touch", confirmed=True)
    assert result["ok"] is True and {c.data["entity_id"] for c in unlocks} == {"lock.door", "lock.back"}


async def test_the_option_does_not_waive_the_pin_for_anything_but_a_lock(hass: HomeAssistant, rig: Rig) -> None:
    hass.states.async_set("cover.garage", "closed", {"device_class": "garage"})
    hass.states.async_set("siren.hall", "off")
    opened = async_mock_service(hass, "cover", "open_cover")
    await rig.start(
        ["cover.garage", "siren.hall"], ["cover"], **with_pin(**{OPT_SENSITIVE: ["cover.garage", "siren.hall"], OPT_UNLOCK_CONFIRM_ONLY: True})
    )
    assert (await ask(rig.session, ["cover.garage"], "open_cover", origin="touch", confirmed=True))["code"] == "pin_required"
    assert (await ask(rig.session, ["siren.hall"], "turn_off", origin="touch", confirmed=True))["code"] == "pin_required"
    assert not opened


async def test_pausing_control_stops_the_confirmation_only_unlock(hass: HomeAssistant, rig: Rig) -> None:
    unlocks = await confirm_only_rig(hass, rig, pin=False)
    rig.hub.set_paused(True)
    await hass.async_block_till_done()
    snapshot = await rig.session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot" and m["policy"]["paused"] is True)
    assert all("ctl_pin" not in e and not e.get("ctl") for e in snapshot["entities"])
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True))["code"] == "paused"
    assert not unlocks


async def test_wrong_pins_lock_out_the_confirmation_only_unlock_too(hass: HomeAssistant, rig: Rig) -> None:
    unlocks = await confirm_only_rig(hass, rig, pin=True, **{OPT_SENSITIVE: ["lock.door"]})
    for _ in range(5):
        await ask(rig.session, ["lock.door"], "open", origin="touch", confirmed=True, pin="1111")
    assert (await ask(rig.session, ["lock.door"], "unlock", origin="touch", confirmed=True))["code"] == "locked_out"
    assert not unlocks
