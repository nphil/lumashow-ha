"""What the Show is given: nothing by default, exactly the list, and only what its domain allows to leave."""

from __future__ import annotations

import asyncio

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumashow.const import (
    DOMAIN,
    OPT_CONTROL_DOMAINS,
    OPT_ENTITIES,
    OPT_FORWARD_NOTIFICATIONS,
    OPT_SHARE_STREAMS,
)

from .conftest import entry_data, setup
from .fake_show import FakeShow


def snapshot_of(session, rev: int | None = None):
    return session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot" and (rev is None or m["rev"] >= rev))


async def test_nothing_is_shared_until_the_person_adds_something(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    hass.states.async_set("light.kitchen", "on", {"brightness": 100})
    hass.states.async_set("lock.front_door", "locked")
    await setup(hass, entry)
    session = await show.wait_session()
    hello = await session.wait_kind("core", "hello")
    assert hello["proto"] == [1]
    assert hello["modules"] == {"states": 1, "actions": 1, "cameras": 1, "notify": 1}
    assert hello["client"]["name"] == DOMAIN and hello["client"]["entry"] == entry.entry_id
    snapshot = await snapshot_of(session)
    assert snapshot["entities"] == [] and snapshot["areas"] == []
    assert snapshot["policy"] == {"paused": False, "pin_set": False}
    assert entry.runtime_data.shared_count == 0


async def test_the_show_gets_exactly_the_list_the_allowed_attributes_and_only_the_names_of_their_areas(
    hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry
) -> None:
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    areas.async_create("Bedroom (shared with nothing)")
    registry = er.async_get(hass)
    registry.async_get_or_create("light", "test", "k1", suggested_object_id="kitchen", original_name="Kitchen light")
    registry.async_update_entity("light.kitchen", area_id=kitchen.id)
    registry.async_get_or_create("sensor", "test", "t1", suggested_object_id="hall_temp", original_name="Hall temperature")
    hass.states.async_set(
        "light.kitchen",
        "on",
        {"brightness": 100, "friendly_name": "Kitchen light", "access_token": "secret", "supported_color_modes": ["xy"], "long": "x" * 300},
    )
    hass.states.async_set("sensor.hall_temp", "21.5", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set("lock.front_door", "locked", {"friendly_name": "Front door"})
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["light.kitchen", "sensor.hall_temp"]})
    await setup(hass, entry)
    session = await show.wait_session()
    snapshot = await snapshot_of(session)
    ids = [e["id"] for e in snapshot["entities"]]
    assert ids == ["light.kitchen", "sensor.hall_temp"], "exactly the list, and the lock that is not on it is not there"
    light = snapshot["entities"][0]
    assert light["area"] == kitchen.id and light["state"] == "on"
    assert light["attrs"] == {"brightness": 100, "friendly_name": "Kitchen light", "supported_color_modes": ["xy"]}
    assert "ctl" not in light, "read-only: the Show was told it may do nothing"
    assert snapshot["areas"] == [{"id": kitchen.id, "name": "Kitchen"}], "the names of the shared entities' areas, and no other"
    assert "access_token" not in str(snapshot) and "secret" not in str(snapshot)
    assert "front_door" not in str(snapshot) and "Bedroom" not in str(snapshot)


async def test_the_verbs_the_show_is_told_follow_what_the_person_allowed(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    hass.states.async_set("light.a", "on")
    hass.states.async_set("switch.b", "off")
    hass.states.async_set("lock.door", "locked")
    hass.config_entries.async_update_entry(
        entry,
        options={OPT_ENTITIES: ["light.a", "switch.b", "lock.door"], OPT_CONTROL_DOMAINS: ["light", "lock"]},
    )
    await setup(hass, entry)
    snapshot = await snapshot_of(await show.wait_session())
    by_id = {e["id"]: e for e in snapshot["entities"]}
    assert by_id["light.a"]["ctl"] == ["turn_on", "turn_off", "toggle"]
    assert "ctl" not in by_id["switch.b"]
    assert by_id["lock.door"]["ctl"] == ["lock"] and "ctl_pin" not in by_id["lock.door"], "unlocking is not offered"


async def test_a_change_of_state_is_pushed_once_and_a_rewrite_of_the_same_value_is_not(
    hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry
) -> None:
    hass.states.async_set("sensor.power", "10", {"unit_of_measurement": "W"})
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["sensor.power"]})
    await setup(hass, entry)
    session = await show.wait_session()
    await snapshot_of(session)
    hass.states.async_set("sensor.power", "12", {"unit_of_measurement": "W"})
    update = await session.wait(lambda m: m["m"] == "states" and m["t"] == "update")
    assert update["entities"][0]["id"] == "sensor.power" and update["entities"][0]["state"] == "12"
    count = len(session.kinds("states", "update"))
    # The same state again, and an attribute the Show never gets: nothing goes out.
    hass.states.async_set("sensor.power", "12", {"unit_of_measurement": "W", "internal_counter": 7})
    await asyncio.sleep(0.5)
    assert len(session.kinds("states", "update")) == count


async def test_an_entity_that_is_not_shared_is_not_pushed_when_it_changes(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    hass.states.async_set("sensor.shared", "1")
    hass.states.async_set("sensor.private", "1")
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["sensor.shared"]})
    await setup(hass, entry)
    session = await show.wait_session()
    await snapshot_of(session)
    hass.states.async_set("sensor.private", "2")
    hass.states.async_set("sensor.shared", "2")
    update = await session.wait(lambda m: m["m"] == "states" and m["t"] == "update")
    assert [e["id"] for e in update["entities"]] == ["sensor.shared"]
    assert "sensor.private" not in str(session.received)


async def test_a_resync_gets_a_fresh_snapshot(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    hass.states.async_set("sensor.a", "1")
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["sensor.a"]})
    await setup(hass, entry)
    session = await show.wait_session()
    first = await snapshot_of(session)
    await session.send({"m": "states", "t": "resync"})
    second = await snapshot_of(session, first["rev"] + 1)
    assert second["entities"][0]["id"] == "sensor.a"


async def test_a_large_set_is_sent_in_parts_that_fit(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    ids = []
    for index in range(240):
        entity_id = f"sensor.s{index}"
        hass.states.async_set(entity_id, "x" * 100, {"friendly_name": "n" * 100, "unit_of_measurement": "u" * 100, "icon": "i" * 100})
        ids.append(entity_id)
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ids})
    await setup(hass, entry)
    session = await show.wait_session()
    await session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot" and not m["more"])
    parts = session.kinds("states", "snapshot")
    assert len(parts) >= 2 and [p["part"] for p in parts] == list(range(len(parts)))
    assert all(p["more"] for p in parts[:-1]) and not parts[-1]["more"]
    assert sum(len(p["entities"]) for p in parts) == 240


async def test_a_rename_in_the_registry_is_followed(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("light", "test", "k1", suggested_object_id="kitchen")
    hass.states.async_set("light.kitchen", "on")
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["light.kitchen"]})
    await setup(hass, entry)
    session = await show.wait_session()
    first = await snapshot_of(session)
    registry.async_update_entity("light.kitchen", new_entity_id="light.kitchen_main")
    hass.states.async_set("light.kitchen_main", "on")
    second = await snapshot_of(session, first["rev"] + 1)
    assert [e["id"] for e in second["entities"]] == ["light.kitchen_main"]
    assert entry.options[OPT_ENTITIES] == ["light.kitchen_main"]


async def test_the_shows_area_for_an_entity_comes_from_its_device_when_it_has_none_of_its_own(
    hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry
) -> None:
    areas = ar.async_get(hass)
    hall = areas.async_create("Hall")
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={("test", "dev")}, name="Hall sensor")
    dr.async_get(hass).async_update_device(device.id, area_id=hall.id)
    er.async_get(hass).async_get_or_create("binary_sensor", "test", "m1", suggested_object_id="hall_motion", device_id=device.id)
    hass.states.async_set("binary_sensor.hall_motion", "off")
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["binary_sensor.hall_motion"]})
    await setup(hass, entry)
    snapshot = await snapshot_of(await show.wait_session())
    assert snapshot["entities"][0]["area"] == hall.id and snapshot["areas"][0]["name"] == "Hall"


async def test_a_show_that_goes_away_is_looked_for_again_and_given_the_set_again(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    hass.states.async_set("sensor.a", "1")
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["sensor.a"]})
    hub = await setup(hass, entry)
    first = await show.wait_session()
    await snapshot_of(first)
    assert hub.connected
    first.writer.close()
    await first.wait_closed()
    second = await show.wait_session(count=2)
    snapshot = await snapshot_of(second)
    assert snapshot["entities"][0]["id"] == "sensor.a" and hub.connected


async def test_unloading_the_entry_tells_the_show_and_cuts_it_off(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    hass.states.async_set("sensor.a", "1")
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["sensor.a"]})
    hub = await setup(hass, entry)
    session = await show.wait_session()
    await snapshot_of(session)
    assert await hass.config_entries.async_unload(entry.entry_id)
    bye = await session.wait_kind("core", "bye")
    assert bye["reason"] == "unloaded"
    await session.wait_closed()
    assert not hub.connected and entry.state is ConfigEntryState.NOT_LOADED
    # Nothing connects again: the entry is not loaded.
    await asyncio.sleep(0.4)
    assert len(show.sessions) == 1


async def test_disabling_the_entry_cuts_the_show_off_at_once(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    hub = await setup(hass, entry)
    session = await show.wait_session()
    await snapshot_of(session)
    await hass.config_entries.async_set_disabled_by(entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    bye = await session.wait_kind("core", "bye")
    assert bye["reason"] == "disabled"
    await session.wait_closed()
    assert not hub.connected


async def test_a_wrong_key_starts_a_reauth_and_stops_trying(hass: HomeAssistant, show: FakeShow) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Show", unique_id="aa:bb:cc:dd:ee:ff", data=entry_data(show.port, bytes(range(1, 33))), options={})
    await setup(hass, entry)
    await asyncio.sleep(0.6)
    assert show.refused == 1, "refused once, not tried in a loop"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_a_show_that_is_not_there_does_not_stop_the_entry_from_loading(
    hass: HomeAssistant, entry: MockConfigEntry, socket_enabled: None
) -> None:
    hass.config_entries.async_update_entry(entry, data={**entry.data, "port": 1})
    hub = await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED and not hub.connected
    assert hass.states.get("binary_sensor.living_room_show_connected").state == "off"


async def test_the_camera_list_has_signed_expiring_addresses_and_no_stream_unless_allowed(
    hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry
) -> None:
    hass.config.internal_url = "http://ha.local:8123"
    hass.states.async_set(
        "camera.front_door", "idle", {"friendly_name": "Front door", "access_token": "TOKEN", "entity_picture": "/api/x?token=TOKEN"}
    )
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["camera.front_door"]})
    await setup(hass, entry)
    session = await show.wait_session()
    listing = await session.wait_kind("cameras", "list")
    camera = listing["cameras"][0]
    assert camera["id"] == "camera.front_door" and "streams" not in camera
    url = camera["snapshot"]["url"]
    assert url.startswith("http://ha.local:8123/api/camera_proxy/camera.front_door?authSig=")
    assert "TOKEN" not in str(listing)
    assert camera["snapshot"]["expires"] > 1_700_000_000
    snapshot = await snapshot_of(session)
    assert "TOKEN" not in str(snapshot), "the camera's own token never leaves"


async def test_camera_addresses_are_shared_only_when_the_person_turned_that_on(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    from unittest.mock import patch

    import homeassistant.components.camera  # noqa: F401

    hass.config.internal_url = "http://ha.local:8123"
    hass.states.async_set("camera.front_door", "idle")
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["camera.front_door"], OPT_SHARE_STREAMS: True})
    with patch("homeassistant.components.camera.async_get_stream_source", return_value="rtsp://user:pw@10.0.0.9/stream"):
        await setup(hass, entry)
        session = await show.wait_session()
        listing = await session.wait_kind("cameras", "list")
    assert listing["cameras"][0]["streams"] == [{"kind": "rtsp", "url": "rtsp://user:pw@10.0.0.9/stream"}]


async def test_a_refresh_of_cameras_gets_a_partial_list(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    hass.config.internal_url = "http://ha.local:8123"
    hass.states.async_set("camera.a", "idle")
    hass.states.async_set("camera.b", "idle")
    hass.config_entries.async_update_entry(entry, options={OPT_ENTITIES: ["camera.a", "camera.b"]})
    await setup(hass, entry)
    session = await show.wait_session()
    await session.wait_kind("cameras", "list")
    await session.send({"m": "cameras", "t": "refresh", "ids": ["camera.b", "camera.not_shared"]})
    partial = await session.wait(lambda m: m["m"] == "cameras" and m["t"] == "list" and m["partial"])
    assert [c["id"] for c in partial["cameras"]] == ["camera.b"]


async def test_persistent_notifications_go_to_the_show_only_when_the_person_turned_that_on(
    hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry
) -> None:
    from homeassistant.components import persistent_notification

    assert await hass.config.async_set_time_zone("UTC") is None or True
    hass.config.components.add("persistent_notification")
    persistent_notification.async_create(hass, "The battery is low", title="Battery", notification_id="n1")
    await setup(hass, entry)
    session = await show.wait_session()
    snapshot = await session.wait_kind("notify", "snapshot")
    assert snapshot["items"] == [], "off by default: nothing of Home Assistant's notifications is sent"
    persistent_notification.async_create(hass, "Another", notification_id="n2")
    await asyncio.sleep(0.3)
    assert not session.kinds("notify", "post")


async def test_forwarded_persistent_notifications_are_snapshot_post_and_clear(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
    from homeassistant.components import persistent_notification

    hass.config.components.add("persistent_notification")
    persistent_notification.async_create(hass, "The battery is low", title="Battery", notification_id="n1")
    hass.config_entries.async_update_entry(entry, options={OPT_FORWARD_NOTIFICATIONS: True})
    await setup(hass, entry)
    session = await show.wait_session()
    snapshot = await session.wait_kind("notify", "snapshot")
    assert snapshot["items"][0]["id"] == "n1" and snapshot["items"][0]["title"] == "Battery"
    assert snapshot["items"][0]["source"] == "persistent_notification"
    persistent_notification.async_create(hass, "Leak!", title="Water", notification_id="n2")
    post = await session.wait_kind("notify", "post")
    assert post["id"] == "n2" and post["message"] == "Leak!"
    persistent_notification.async_dismiss(hass, "n2")
    clear = await session.wait_kind("notify", "clear")
    assert clear["id"] == "n2"
