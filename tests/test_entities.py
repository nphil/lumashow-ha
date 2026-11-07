"""The entities the integration adds to a Show's device: what they say, the switch that stops the Show, the message, the diagnostics."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from homeassistant.const import STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockConfigEntry, async_mock_service

from custom_components.lumashow import hub as hub_module
from custom_components.lumashow.const import (
    CONF_KEY,
    DOMAIN,
    OPT_CONTROL_DOMAINS,
    OPT_ENTITIES,
    OPT_PIN_HASH,
    OPT_PIN_SALT,
)
from custom_components.lumashow.diagnostics import async_get_config_entry_diagnostics
from custom_components.lumashow.entity import LumaShowEntity

from .conftest import MAC, PAIRING_KEY, entry_data, settle, setup
from .fake_show import FakeShow
from .rig import Rig, ask, light, with_pin
from .test_hub import snapshot_of


def entity_of(hass: HomeAssistant, platform: str, key: str) -> str:
    found = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{MAC}_{key}")
    assert found, f"no {platform} entity {key}"
    return found


async def until_state(hass: HomeAssistant, entity_id: str, wanted: str, seconds: float = 3.0) -> None:
    async with asyncio.timeout(seconds):
        while (state := hass.states.get(entity_id)) is None or state.state != wanted:
            await asyncio.sleep(0.02)


async def test_a_show_is_one_device_with_its_diagnostic_entities_a_switch_and_a_notify_entity(
    hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry
) -> None:
    await setup(hass, entry)
    await show.wait_session()
    await until_state(hass, entity_of(hass, "binary_sensor", "connected"), "on")  # the welcome has been read: the device page is whole
    registry = er.async_get(hass)
    expected = {
        ("binary_sensor", "connected"): EntityCategory.DIAGNOSTIC,
        ("sensor", "entities_shared"): EntityCategory.DIAGNOSTIC,
        ("sensor", "last_push"): EntityCategory.DIAGNOSTIC,
        ("sensor", "actions"): EntityCategory.DIAGNOSTIC,
        ("switch", "pause_control"): None,
        ("notify", "message"): None,
    }
    device_ids = set()
    for (platform, key), category in expected.items():
        registered = registry.async_get(entity_of(hass, platform, key))
        assert registered is not None and registered.entity_category == category, (platform, key)
        device_ids.add(registered.device_id)
    assert len(device_ids) == 1, "all of them under the Show's one device"
    device = dr.async_get(hass).async_get(device_ids.pop())
    assert device is not None
    assert (DOMAIN, MAC) in device.identifiers, "the ESPHome entries of the same Show share the hardware address"
    assert (dr.CONNECTION_NETWORK_MAC, MAC) in device.connections
    assert (device.manufacturer, device.model, device.sw_version) == ("LumaShow", "Echo Show 5", "v1-test")


async def test_connected_follows_the_channel_and_comes_back_with_it(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    first = await show.wait_session()
    await snapshot_of(first)
    connected = entity_of(hass, "binary_sensor", "connected")
    await until_state(hass, connected, "on")
    # The Show goes, and comes back but does not say who it is: the channel is not agreed, so it is not connected.
    show.answer_hello = False
    first.writer.close()
    await first.wait_closed()
    await show.wait_session(count=2)
    await until_state(hass, connected, "off")
    assert entry.runtime_data.connected is False


async def test_connected_says_off_while_nobody_answers(hass: HomeAssistant, socket_enabled: None) -> None:
    unused_port = 1  # nothing listens there: the connection is refused at once
    entry = MockConfigEntry(domain=DOMAIN, title="Living Room Show", unique_id=MAC, data=entry_data(unused_port), options={})
    entry.add_to_hass(hass)
    await setup(hass, entry)
    await settle(hass, 0.3)
    state = hass.states.get(entity_of(hass, "binary_sensor", "connected"))
    assert state is not None and state.state == "off"
    assert entry.runtime_data.connected is False


async def test_entities_shared_counts_what_is_given_and_what_may_be_controlled(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    hass.states.async_set("sensor.b", "5")
    await rig.start(["light.a", "sensor.b"], ["light"])
    state = hass.states.get(entity_of(hass, "sensor", "entities_shared"))
    assert state is not None and state.state == "2"
    assert state.attributes["controllable"] == 1 and state.attributes["control_paused"] is False


async def test_last_push_is_a_time_to_the_minute_and_not_a_new_value_for_every_state(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    await rig.start(["light.a"], ["light"])
    state = hass.states.get(entity_of(hass, "sensor", "last_push"))
    assert state is not None
    pushed = dt_util.parse_datetime(state.state)
    assert pushed is not None and pushed.second == 0 and pushed.microsecond == 0
    for level in range(5):
        hass.states.async_set("light.a", "on", {"friendly_name": "Kitchen light", "brightness": 10 + level})
    await settle(hass, 0.5)
    again = hass.states.get(entity_of(hass, "sensor", "last_push"))
    assert again is not None and dt_util.parse_datetime(again.state).second == 0


async def test_the_first_push_is_shown_even_when_the_machine_was_started_a_few_seconds_ago(
    hass: HomeAssistant, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The throttle of the sensor counts on the machine's monotonic clock, which starts at boot: 5 s is a Home Assistant that has just started.
    monkeypatch.setattr(hub_module, "time", SimpleNamespace(monotonic=lambda: 5.0, time=time.time))
    light(hass)
    await rig.start(["light.a"], ["light"])
    entity_id = entity_of(hass, "sensor", "last_push")
    async with asyncio.timeout(3):
        while (state := hass.states.get(entity_id)) is None or state.state == STATE_UNKNOWN:
            await asyncio.sleep(0.02)
    assert dt_util.parse_datetime(state.state) is not None


async def test_actions_counts_what_was_carried_out_and_says_how_many_it_refused(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    async_mock_service(hass, "light", "turn_on")
    await rig.start(["light.a"], ["light"])
    counter = entity_of(hass, "sensor", "actions")
    assert hass.states.get(counter).state == "0"
    assert (await ask(rig.session, ["light.a"], "turn_on"))["ok"] is True
    refused = await ask(rig.session, ["light.a"], "format_the_disk")
    assert refused["ok"] is False
    await settle(hass, 0.2)
    state = hass.states.get(counter)
    assert state is not None and state.state == "1"
    assert state.attributes["refused"] == 1
    assert state.attributes["last_action"] == "refused: unknown_verb"


async def test_an_entity_renamed_by_the_person_keeps_following_the_hub_under_its_new_id_and_is_changed_in_place(
    hass: HomeAssistant, rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    added: list[str] = []
    original = LumaShowEntity.async_added_to_hass

    async def counted(self: LumaShowEntity) -> None:
        added.append(self.entity_id)
        await original(self)

    monkeypatch.setattr(LumaShowEntity, "async_added_to_hass", counted)
    light(hass)
    async_mock_service(hass, "light", "turn_on")
    await rig.start(["light.a"], ["light"])
    before = len(added)
    old = entity_of(hass, "sensor", "actions")
    assert hass.states.get(old).state == "0"
    er.async_get(hass).async_update_entity(old, new_entity_id="sensor.show_actions_renamed")
    await hass.async_block_till_done()
    assert hass.states.get(old) is None, "no state is left under the old id"
    assert hass.states.get("sensor.show_actions_renamed").state == "0"
    if hasattr(
        Entity, "async_entity_id_changed"
    ):  # Home Assistant 2026.11 and later: an entity that says it can be renamed is not taken away and brought back
        assert len(added) == before, "changed in place, async_added_to_hass is not run again"
    assert (await ask(rig.session, ["light.a"], "turn_on"))["ok"] is True
    await settle(hass, 0.2)
    renamed = hass.states.get("sensor.show_actions_renamed")
    assert renamed is not None and renamed.state == "1", "what the hub says reaches the entity under its new id"
    assert hass.states.get(old) is None


async def test_a_message_sent_to_the_notify_entity_is_shown_on_the_show(hass: HomeAssistant, rig: Rig) -> None:
    await rig.start([])
    notify = entity_of(hass, "notify", "message")
    await hass.services.async_call("notify", "send_message", {"entity_id": notify, "message": "Dinner is ready", "title": "Kitchen"}, blocking=True)
    posted = await rig.session.wait_kind("notify", "post")
    assert posted["title"] == "Kitchen" and posted["message"] == "Dinner is ready"
    assert posted["source"] == "service" and posted["level"] == "info" and posted["ttl_s"] == 3600
    # Without a title the Show's own name is the title.
    await hass.services.async_call("notify", "send_message", {"entity_id": notify, "message": "Hello"}, blocking=True)
    second = await rig.session.wait(lambda m: m["m"] == "notify" and m["t"] == "post" and m["message"] == "Hello")
    assert second["title"]


async def test_a_message_for_a_show_that_is_not_connected_is_an_error_not_a_silence(hass: HomeAssistant, rig: Rig) -> None:
    await rig.start([])
    notify = entity_of(hass, "notify", "message")
    rig.session.writer.close()
    await rig.session.wait_closed()
    rig.hub.agreed.clear()  # nothing agreed with whoever might come back
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("notify", "send_message", {"entity_id": notify, "message": "Anybody?"}, blocking=True)


async def test_diagnostics_hold_what_is_needed_to_find_a_fault_and_neither_the_key_nor_the_pin(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    await rig.start(["light.a"], ["light"], **with_pin())
    diagnostics = await async_get_config_entry_diagnostics(hass, rig.entry)
    text = str(diagnostics)
    assert PAIRING_KEY.hex() not in text
    assert rig.entry.data[CONF_KEY] not in text
    assert rig.entry.options[OPT_PIN_HASH] not in text and rig.entry.options[OPT_PIN_SALT] not in text
    assert diagnostics["entry"]["data"][CONF_KEY] == "**REDACTED**"
    assert diagnostics["entry"]["options"][OPT_PIN_HASH] == "**REDACTED**"
    assert diagnostics["entry"]["options"][OPT_ENTITIES] == ["light.a"] and diagnostics["entry"]["options"][OPT_CONTROL_DOMAINS] == ["light"]
    hub = diagnostics["hub"]
    assert hub["connected"] is True and hub["shared"] == 1 and hub["controllable"] == 1 and hub["paused"] is False
    assert hub["agreed_modules"] == {"states": 1, "actions": 1, "cameras": 1, "notify": 1}
