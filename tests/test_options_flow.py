"""Choosing what a Show is given: it starts with nothing, quick starts add once, the list is edited by entity, then control, sensitive actions, the PIN."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumashow.const import (
    OPT_CAMERA_EVENTS,
    OPT_CONTROL_DOMAINS,
    OPT_ENTITIES,
    OPT_FORWARD_NOTIFICATIONS,
    OPT_PIN_HASH,
    OPT_PIN_SALT,
    OPT_READ_ONLY,
    OPT_SENSITIVE,
    OPT_SHARE_ALARMS,
    OPT_SHARE_STREAMS,
)
from custom_components.lumashow.pin import matches as pin_matches
from custom_components.lumashow.pin import new_pin

from .conftest import setup
from .fake_show import FakeShow
from .test_hub import snapshot_of

MENU = ["pick_area", "pick_device", "pick_type", "pick_entities", "copy_assist", "control", "sensitive", "pin", "extras", "save"]


def house(hass: HomeAssistant) -> dict[str, str]:
    """A small house: a kitchen with a hub (a light, a plug, a temperature, a diagnostic, a hidden and a disabled entity, a script) and a hall with a lock and a camera."""
    areas = ar.async_get(hass)
    kitchen, hall = areas.async_create("Kitchen"), areas.async_create("Hall")
    owner = MockConfigEntry(domain="demo_house")
    owner.add_to_hass(hass)
    devices = dr.async_get(hass)
    hub = devices.async_get_or_create(config_entry_id=owner.entry_id, identifiers={("demo_house", "hub")}, name="Kitchen hub")
    devices.async_update_device(hub.id, area_id=kitchen.id)
    registry = er.async_get(hass)

    def make(domain: str, name: str, **kwargs: Any) -> None:
        registry.async_get_or_create(domain, "demo_house", name, suggested_object_id=name, config_entry=owner, **kwargs)
        if kwargs.get("disabled_by") is None:
            hass.states.async_set(f"{domain}.{name}", "on" if domain in {"light", "switch"} else "unknown", {"friendly_name": name})

    make("light", "kitchen_main", device_id=hub.id)
    make("switch", "kitchen_plug", device_id=hub.id)
    make("sensor", "kitchen_temp", device_id=hub.id)
    make("sensor", "kitchen_rssi", device_id=hub.id, entity_category=EntityCategory.DIAGNOSTIC)
    make("light", "kitchen_hidden", device_id=hub.id, hidden_by=er.RegistryEntryHider.USER)
    make("light", "kitchen_off", device_id=hub.id, disabled_by=er.RegistryEntryDisabler.USER)
    make("script", "goodnight")
    registry.async_update_entity("script.goodnight", area_id=kitchen.id)
    make("lock", "front_door")
    registry.async_update_entity("lock.front_door", area_id=hall.id)
    make("camera", "porch")
    registry.async_update_entity("camera.porch", area_id=hall.id)
    return {"kitchen": kitchen.id, "hall": hall.id, "hub": hub.id}


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "init"
    return result


async def go(hass: HomeAssistant, result: dict[str, Any], step: str, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
    """Picks a menu item and, when given, fills in the form it opens (the answer is the next screen)."""
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})
    if user_input is not None:
        assert result["type"] is FlowResultType.FORM and result["step_id"] == step, result
        result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    return result


async def save(hass: HomeAssistant, result: dict[str, Any]) -> dict[str, Any]:
    saved = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "save"})
    assert saved["type"] is FlowResultType.CREATE_ENTRY
    return saved["data"]


def said(result: dict[str, Any]) -> str:
    assert result["type"] is FlowResultType.MENU, result
    return result["description_placeholders"]["summary"]


async def test_a_new_show_has_nothing_shared_and_the_menu_says_so(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    result = await start(hass, entry)
    assert said(result) == "Nothing is shared with this Show yet."
    assert result["menu_options"] == MENU
    assert await save(hass, result) == {}, "saving the empty draft changes nothing"


async def test_a_quick_start_by_area_adds_what_stands_there_once_and_not_scripts_hidden_or_diagnostic_ones(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    ids = house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pick_area", {"areas": [ids["kitchen"]]})
    assert said(result) == "Added 3 from these areas. 3 entities shared, 0 can be controlled."
    # Once: the same again adds nothing, and the list does not grow.
    result = await go(hass, result, "pick_area", {"areas": [ids["kitchen"]]})
    assert said(result).startswith("Added 0 from these areas.")
    data = await save(hass, result)
    assert data[OPT_ENTITIES] == ["light.kitchen_main", "sensor.kitchen_temp", "switch.kitchen_plug"]


async def test_the_quick_start_by_area_can_be_asked_for_kinds_and_for_the_minor_ones(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    ids = house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pick_area", {"areas": [ids["kitchen"]], "types": ["lights"], "include_minor": True})
    assert said(result).startswith("Added 2 from these areas."), "the hidden light too, not the disabled one"
    data = await save(hass, result)
    assert data[OPT_ENTITIES] == ["light.kitchen_hidden", "light.kitchen_main"]


async def test_a_quick_start_by_device_adds_that_devices_entities(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    ids = house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pick_device", {"devices": [ids["hub"]]})
    assert said(result).startswith("Added 3 from these devices.")
    assert (await save(hass, result))[OPT_ENTITIES] == ["light.kitchen_main", "sensor.kitchen_temp", "switch.kitchen_plug"]


async def test_a_quick_start_by_type_names_scripts_only_when_they_are_asked_for_by_name(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    ids = house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pick_type", {"groups": ["locks", "cameras"]})
    assert said(result).startswith("Added 2 from these kinds.")
    result = await go(hass, result, "pick_type", {"groups": ["scripts"]})
    assert said(result).startswith("Added 1 from these kinds.")
    # A kind in one area only.
    result = await go(hass, result, "pick_type", {"groups": ["sensors"], "areas": [ids["hall"]]})
    assert said(result).startswith("Nothing to add from these kinds.")
    data = await save(hass, result)
    assert data[OPT_ENTITIES] == ["camera.porch", "lock.front_door", "script.goodnight"]


async def test_nothing_is_added_later_by_itself_when_an_area_gains_a_device(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    ids = house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pick_area", {"areas": [ids["hall"]]})
    data = await save(hass, result)
    assert data[OPT_ENTITIES] == ["camera.porch", "lock.front_door"]
    # A new light in the hall is not on the list: the list is what was picked, not the room.
    er.async_get(hass).async_get_or_create("light", "demo_house", "hall_new", suggested_object_id="hall_new")
    er.async_get(hass).async_update_entity("light.hall_new", area_id=ids["hall"])
    hass.config_entries.async_update_entry(entry, options=data)
    result = await start(hass, entry)
    assert said(result) == "2 entities shared, 0 can be controlled."


async def test_the_list_is_edited_by_entity_and_what_leaves_it_leaves_every_other_list(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    ids = house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pick_area", {"areas": [ids["kitchen"]]})
    result = await go(hass, result, "pick_type", {"groups": ["locks"]})
    result = await go(hass, result, "control", {OPT_CONTROL_DOMAINS: ["light", "switch"], OPT_READ_ONLY: ["switch.kitchen_plug"]})
    assert said(result).endswith("4 entities shared, 1 can be controlled.")
    result = await go(hass, result, "pick_entities", {OPT_ENTITIES: ["light.kitchen_main", "light.kitchen_main", "lock.front_door"]})
    assert said(result) == "The list has 2. 2 entities shared, 1 can be controlled."
    data = await save(hass, result)
    assert data[OPT_ENTITIES] == ["light.kitchen_main", "lock.front_door"]
    assert data[OPT_READ_ONLY] == [], "the plug left the list, so it left the read-only list too"


async def test_copying_what_assist_may_see_is_one_optional_step_that_adds_nothing_unless_asked(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    for registry_entry in list(er.async_get(hass).entities.values()):
        async_expose_entity(hass, "conversation", registry_entry.entity_id, registry_entry.entity_id in {"light.kitchen_main", "lock.front_door"})
    result = await start(hass, entry)
    result = await go(hass, result, "copy_assist")
    assert result["type"] is FlowResultType.FORM and result["description_placeholders"]["count"] == "2"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"add": False})
    assert said(result) == "Nothing is shared with this Show yet.", "the box is off unless it is turned on"
    result = await go(hass, result, "copy_assist", {"add": True})
    assert said(result).startswith("Added 2 from Assist.")
    assert (await save(hass, result))[OPT_ENTITIES] == ["light.kitchen_main", "lock.front_door"]


async def test_only_domains_with_an_ordinary_verb_are_offered_for_control(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    hass.states.async_set("siren.alarm", "off")
    result = await start(hass, entry)
    result = await go(hass, result, "pick_entities", {OPT_ENTITIES: ["light.kitchen_main", "sensor.kitchen_temp", "siren.alarm"]})
    result = await go(hass, result, "control")
    offered = next(v for k, v in result["data_schema"].schema.items() if str(k) == OPT_CONTROL_DOMAINS)
    assert [o["value"] for o in offered.config["options"]] == ["light"], "a sensor has no verb and a siren has only sensitive ones"
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {OPT_CONTROL_DOMAINS: ["light", "siren"]})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {OPT_CONTROL_DOMAINS: ["light"]})
    data = await save(hass, result)
    assert data[OPT_CONTROL_DOMAINS] == ["light"]


async def test_a_sensitive_action_cannot_be_allowed_before_a_pin_exists(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pick_type", {"groups": ["locks"]})
    result = await go(hass, result, "sensitive")
    assert said(result).startswith("Set a PIN first")
    data = await save(hass, result)
    assert not data.get(OPT_SENSITIVE) and OPT_PIN_HASH not in data


async def test_with_a_pin_a_sensitive_action_can_be_allowed_and_the_pin_itself_is_never_kept(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pick_type", {"groups": ["locks", "cameras"]})
    result = await go(hass, result, "pin", {"new_pin": "2468", "confirm_pin": "2468"})
    assert said(result).startswith("The PIN is set.")
    result = await go(hass, result, "sensitive")
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {OPT_SENSITIVE: ["lock.front_door", "light.not_on_the_list"]})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {OPT_SENSITIVE: ["lock.front_door"]})
    assert said(result) == "Sensitive actions saved in the draft. 2 entities shared, 1 can be controlled."
    data = await save(hass, result)
    assert data[OPT_SENSITIVE] == ["lock.front_door"], "only what is on the list can be asked for"
    assert data[OPT_PIN_HASH] and data[OPT_PIN_SALT] and "2468" not in str(data)


def with_pin(hass: HomeAssistant, entry: MockConfigEntry, pin: str = "2468", **more: Any) -> str:
    """Gives the entry a PIN (as the options flow would have saved it) and says its hash."""
    hashed, salt = new_pin(pin)
    hass.config_entries.async_update_entry(entry, options={OPT_PIN_HASH: hashed, OPT_PIN_SALT: salt, **more})
    return hashed


async def test_a_new_pin_has_rules_and_must_be_typed_twice(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "pin")
    assert [str(key) for key in result["data_schema"].schema] == ["new_pin", "confirm_pin"], "no old PIN is asked for when there is none"
    configure = hass.config_entries.options.async_configure
    flow = result["flow_id"]
    assert (await configure(flow, {"new_pin": "12", "confirm_pin": "12"}))["errors"] == {"new_pin": "pin_format"}
    assert (await configure(flow, {"new_pin": "12a4", "confirm_pin": "12a4"}))["errors"] == {"new_pin": "pin_format"}
    assert (await configure(flow, {"new_pin": "123456789", "confirm_pin": "123456789"}))["errors"] == {"new_pin": "pin_format"}
    assert (await configure(flow, {"new_pin": "2468", "confirm_pin": "1357"}))["errors"] == {"confirm_pin": "pin_mismatch"}
    menu = await configure(flow, {"new_pin": "2468", "confirm_pin": "2468"})
    assert said(menu).startswith("The PIN is set.")


async def test_changing_the_pin_needs_the_old_one(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    old = with_pin(hass, entry)
    result = await start(hass, entry)
    result = await go(hass, result, "pin")
    assert [str(key) for key in result["data_schema"].schema] == ["current_pin", "new_pin", "confirm_pin"]
    configure = hass.config_entries.options.async_configure
    flow = result["flow_id"]
    wrong = await configure(flow, {"current_pin": "0000", "new_pin": "1357", "confirm_pin": "1357"})
    assert wrong["errors"] == {"current_pin": "pin_wrong"}
    menu = await configure(flow, {"current_pin": "2468", "new_pin": "1357", "confirm_pin": "1357"})
    assert said(menu).startswith("The PIN is set.")
    data = await save(hass, menu)
    assert data[OPT_PIN_HASH] != old and "1357" not in str(data)
    assert pin_matches("1357", data) and not pin_matches("2468", data)


async def test_removing_the_pin_needs_the_old_one_and_takes_the_sensitive_actions_with_it(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    with_pin(hass, entry, **{OPT_ENTITIES: ["lock.front_door"], OPT_SENSITIVE: ["lock.front_door"]})
    result = await start(hass, entry)
    assert said(result) == "1 entity shared, 1 can be controlled."
    result = await go(hass, result, "pin")
    configure = hass.config_entries.options.async_configure
    flow = result["flow_id"]
    refused = await configure(flow, {"current_pin": "9999", "new_pin": "", "confirm_pin": ""})
    assert refused["errors"] == {"current_pin": "pin_wrong"}
    menu = await configure(flow, {"current_pin": "2468", "new_pin": "", "confirm_pin": ""})
    assert said(menu) == "The PIN is removed, and the sensitive actions with it. 1 entity shared, 0 can be controlled."
    data = await save(hass, menu)
    assert OPT_PIN_HASH not in data and OPT_PIN_SALT not in data and data[OPT_SENSITIVE] == []


async def test_the_extras_are_off_until_turned_on(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    house(hass)
    result = await start(hass, entry)
    result = await go(hass, result, "extras")
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults == {OPT_FORWARD_NOTIFICATIONS: False, OPT_SHARE_STREAMS: False, OPT_SHARE_ALARMS: False, OPT_CAMERA_EVENTS: False}
    result = await hass.config_entries.options.async_configure(result["flow_id"], {OPT_FORWARD_NOTIFICATIONS: True})
    data = await save(hass, result)
    assert data[OPT_FORWARD_NOTIFICATIONS] is True and data[OPT_SHARE_STREAMS] is False and data[OPT_SHARE_ALARMS] is False
    assert data[OPT_CAMERA_EVENTS] is False


async def test_saving_reloads_the_entry_and_the_show_is_given_what_was_picked(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    ids = house(hass)
    await setup(hass, entry)
    first = await show.wait_session()
    assert (await snapshot_of(first))["entities"] == []
    result = await start(hass, entry)
    result = await go(hass, result, "pick_area", {"areas": [ids["hall"]]})
    result = await go(hass, result, "control", {OPT_CONTROL_DOMAINS: ["camera"]})
    await save(hass, result)
    await hass.async_block_till_done()
    second = await show.wait_session(count=2)
    snapshot = await snapshot_of(second)
    by_id = {e["id"]: e for e in snapshot["entities"]}
    assert sorted(by_id) == ["camera.porch", "lock.front_door"]
    assert snapshot["areas"] == [{"id": ids["hall"], "name": "Hall"}], "the hall's name, and not the kitchen's"
    assert "ctl" in by_id["camera.porch"] and "ctl" not in by_id["lock.front_door"]
