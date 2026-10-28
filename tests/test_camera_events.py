"""Module ``camera_events``: which detectors count for a shared camera, one event once, the hold, and the channel through the real hub."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumashow.camera_events import HOLD, CameraEventsModule, classify, kind_of_words
from custom_components.lumashow.const import DOMAIN, OPT_CAMERA_EVENTS, OPT_ENTITIES
from custom_components.lumashow.selection import Shared

from .conftest import MAC, PAIRING_KEY, entry_data, setup
from .fake_show import FakeShow

CAMERA = "camera.front_door"


class FakeHub:
    """What the module needs of a hub: its name, its options, what it shares, and a way to send."""

    def __init__(self, cameras: dict[str, str], *, sharing: bool = True) -> None:
        self.name = "Living Room TV"
        self.entry = SimpleNamespace(entry_id="entry-tv")
        self.options = {OPT_CAMERA_EVENTS: sharing}
        self.agreed = {"camera_events": 1}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._shared = {entity_id: Shared(entity_id=entity_id, name=name, area_id=None, minor=False) for entity_id, name in cameras.items()}

    def shared_entities(self) -> dict[str, Shared]:
        return self._shared

    async def async_send_module(self, module: str, kind: str, fields: Any) -> bool:
        assert module == "camera_events"
        self.sent.append((kind, dict(fields)))
        return True

    def spawn_module_send(self, module: str, kind: str, fields: Any) -> None:
        self.sent.append((kind, dict(fields)))

    def kinds(self, kind: str) -> list[dict[str, Any]]:
        return [fields for k, fields in self.sent if k == kind]


def test_a_detectors_name_or_class_says_what_it_announces() -> None:
    assert classify("binary_sensor.front_doorbell_camera_person", "Person", "motion", None) == "person"
    assert classify("binary_sensor.front_doorbell_camera_vehicle", "Vehicle", "motion", None) == "vehicle"
    assert classify("binary_sensor.front_doorbell_camera_pet", "Pet", "motion", None) == "animal"
    assert classify("binary_sensor.front_doorbell_camera_visitor", "Visitor", "occupancy", None) == "doorbell"
    assert classify("binary_sensor.front_doorbell_camera_package", "Package", "occupancy", None) == "package"
    assert classify("binary_sensor.front_doorbell_camera_motion", "Motion", "motion", None) == "motion"
    assert classify("binary_sensor.exterior_front_door_camera_people_detected", "People detected", "motion", None) == "person"
    assert classify("binary_sensor.exterior_front_door_camera_face_detected", "Face detected", "motion", None) == "person"
    assert classify("event.exterior_front_door_camera_doorbell", "Doorbell", "doorbell", ["ring"]) == "doorbell"
    # Many classes in one event entity: read at each event.
    assert (
        classify(
            "event.exterior_front_door_camera_object_detected", "Object detected", "motion", ["package", "people", "vehicle", "person", "animal"]
        )
        == "*"
    )
    # An animal watcher's event entity, named for its kinds.
    assert (
        classify("event.kestrel_front_door_camera_animal", "Kestrel Front Door Camera animal", None, ["bird", "mammal", "unidentified"]) == "animal"
    )
    # Not detectors.
    assert classify("binary_sensor.exterior_front_door_camera_online", "Online", "connectivity", None) is None
    assert classify("binary_sensor.exterior_front_door_camera_sound", "Sound", "sound", None) is None
    assert classify("binary_sensor.exterior_front_door_camera_binary_state", "Binary state", None, None) is None
    assert kind_of_words("dog_cat") == "animal" and kind_of_words("Domestic Cat") == "animal" and kind_of_words("ring") == "doorbell"


def _camera_device(hass: HomeAssistant, entry_id: str) -> None:
    """A camera with its detectors on one device, as the Reolink and Scrypted integrations make them."""
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry_id, identifiers={("test", "cam")}, name="Front Door Camera")
    registry = er.async_get(hass)
    registry.async_get_or_create("camera", "test", "cam", suggested_object_id="front_door", device_id=device.id)
    for unique, object_id, device_class in (
        ("p", "front_door_person", "motion"),
        ("v", "front_door_vehicle", "motion"),
        ("o", "front_door_online", "connectivity"),
    ):
        registry.async_get_or_create(
            "binary_sensor", "test", unique, suggested_object_id=object_id, device_id=device.id, original_device_class=device_class
        )
    registry.async_get_or_create(
        "event", "test", "obj", suggested_object_id="front_door_object_detected", device_id=device.id, original_device_class="motion"
    )
    registry.async_get_or_create(
        "event", "test", "bell", suggested_object_id="front_door_doorbell", device_id=device.id, original_device_class="doorbell"
    )
    hass.states.async_set(CAMERA, "idle", {"friendly_name": "Front Door Camera"})
    hass.states.async_set("binary_sensor.front_door_person", "off", {"device_class": "motion"})
    hass.states.async_set("binary_sensor.front_door_vehicle", "off", {"device_class": "motion"})
    hass.states.async_set("binary_sensor.front_door_online", "on", {"device_class": "connectivity"})
    hass.states.async_set(
        "event.front_door_object_detected", "unknown", {"device_class": "motion", "event_types": ["package", "people", "vehicle", "person", "animal"]}
    )
    hass.states.async_set("event.front_door_doorbell", "unknown", {"device_class": "doorbell", "event_types": ["ring"]})
    # A watcher elsewhere that names the camera it looked through.
    hass.states.async_set(
        "event.kestrel_front_door_camera_animal",
        "unknown",
        {"event_types": ["bird", "mammal", "unidentified"], "camera": "Front Door Camera", "friendly_name": "Kestrel Front Door Camera animal"},
    )
    # A detector of another camera: never a source of this one.
    hass.states.async_set("binary_sensor.back_door_person", "off", {"device_class": "motion"})


async def test_the_detectors_of_a_shared_camera_are_watched_and_an_event_is_sent_once(hass: HomeAssistant) -> None:
    config = MockConfigEntry(domain="test")
    config.add_to_hass(hass)
    _camera_device(hass, config.entry_id)
    module = CameraEventsModule(hass)
    hub = FakeHub({CAMERA: "Front Door Camera"})
    await module.async_session(hub, True)  # type: ignore[arg-type]
    assert module.sources_of(hub) == {  # type: ignore[arg-type]
        CAMERA: [
            "binary_sensor.front_door_person",
            "binary_sensor.front_door_vehicle",
            "event.front_door_doorbell",
            "event.front_door_object_detected",
            "event.kestrel_front_door_camera_animal",
        ]
    }
    assert hub.kinds("list") == [{"cameras": [{"id": CAMERA, "kinds": ["person", "vehicle", "animal", "package", "doorbell"]}]}]

    clock = [100.0]
    module._watches["entry-tv"].clock = lambda: clock[0]
    hass.states.async_set("binary_sensor.front_door_person", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    events = hub.kinds("event")
    assert len(events) == 1
    assert events[0]["camera"] == CAMERA and events[0]["kind"] == "person" and events[0]["source"] == "binary_sensor.front_door_person"
    assert isinstance(events[0]["at"], float) and "label" not in events[0]
    # Off and on again within the hold: one event. After it: another.
    hass.states.async_set("binary_sensor.front_door_person", "off", {"device_class": "motion"})
    hass.states.async_set("binary_sensor.front_door_person", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    assert len(hub.kinds("event")) == 1
    clock[0] += HOLD + 0.1
    hass.states.async_set("binary_sensor.front_door_person", "off", {"device_class": "motion"})
    hass.states.async_set("binary_sensor.front_door_person", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    assert len(hub.kinds("event")) == 2
    # An attribute rewrite while on is not an event; the online sensor and another camera's detector are not sources.
    hass.states.async_set("binary_sensor.front_door_person", "on", {"device_class": "motion", "x": 1})
    hass.states.async_set("binary_sensor.front_door_online", "off", {"device_class": "connectivity"})
    hass.states.async_set("binary_sensor.back_door_person", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    assert len(hub.kinds("event")) == 2
    # An event entity with many classes is read at the event; a doorbell is a doorbell; the watcher's species is the label.
    clock[0] += HOLD + 0.1
    hass.states.async_set(
        "event.front_door_object_detected",
        "2026-10-28T02:24:34.040+00:00",
        {"device_class": "motion", "event_types": ["package", "people", "vehicle", "person", "animal"], "event_type": "vehicle"},
    )
    hass.states.async_set(
        "event.front_door_doorbell", "2026-10-28T02:24:35.000+00:00", {"device_class": "doorbell", "event_types": ["ring"], "event_type": "ring"}
    )
    hass.states.async_set(
        "event.kestrel_front_door_camera_animal",
        "2026-10-28T02:24:36.000+00:00",
        {"event_types": ["bird", "mammal", "unidentified"], "event_type": "mammal", "species": "Domestic Cat", "camera": "Front Door Camera"},
    )
    await hass.async_block_till_done()
    kinds = [(e["kind"], e.get("label")) for e in hub.kinds("event")[2:]]
    assert kinds == [("vehicle", None), ("doorbell", None), ("animal", "Domestic Cat")]
    # The session ends: nothing is watched any more.
    await module.async_session(hub, False)  # type: ignore[arg-type]
    clock[0] += HOLD + 0.1
    hass.states.async_set("binary_sensor.front_door_vehicle", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    assert len(hub.kinds("event")) == 5 and module.sources_of(hub) == {}  # type: ignore[arg-type]


async def test_nothing_runs_for_a_device_that_does_not_share_camera_events(hass: HomeAssistant) -> None:
    config = MockConfigEntry(domain="test")
    config.add_to_hass(hass)
    _camera_device(hass, config.entry_id)
    module = CameraEventsModule(hass)
    hub = FakeHub({CAMERA: "Front Door Camera"}, sharing=False)
    assert module.offers(hub) is False  # type: ignore[arg-type]
    await module.async_session(hub, True)  # type: ignore[arg-type]
    hass.states.async_set("binary_sensor.front_door_person", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    assert hub.sent == []


async def test_the_detectors_follow_the_shared_set(hass: HomeAssistant) -> None:
    config = MockConfigEntry(domain="test")
    config.add_to_hass(hass)
    _camera_device(hass, config.entry_id)
    module = CameraEventsModule(hass)
    hub = FakeHub({})
    await module.async_session(hub, True)  # type: ignore[arg-type]
    assert hub.kinds("list") == [{"cameras": []}]
    hub._shared[CAMERA] = Shared(entity_id=CAMERA, name="Front Door Camera", area_id=None, minor=False)
    await module.async_shared_changed(hub)  # type: ignore[arg-type]
    assert hub.kinds("list")[-1]["cameras"][0]["id"] == CAMERA
    await module.async_shared_changed(hub)  # type: ignore[arg-type]
    assert len(hub.kinds("list")) == 2, "an unchanged set is not listed again"
    await module.async_message(hub, "refresh", {})  # type: ignore[arg-type]
    assert len(hub.kinds("list")) == 3


# -- through the real hub -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("shares", [True, False])
async def test_camera_events_are_offered_only_to_a_device_that_shares_them_and_reach_it_through_the_channel(
    hass: HomeAssistant, socket_enabled: None, shares: bool
) -> None:
    fake = FakeShow(PAIRING_KEY, modules={"states": 1, "actions": 1, "cameras": 1, "notify": 1, "camera_events": 1})
    await fake.start()
    options = {OPT_ENTITIES: [CAMERA], OPT_CAMERA_EVENTS: shares}
    config = MockConfigEntry(domain=DOMAIN, title="Living Room TV", unique_id=MAC, data=entry_data(fake.port), options=options)
    config.add_to_hass(hass)
    _camera_device(hass, config.entry_id)
    try:
        await setup(hass, config)
        session = await fake.wait_session()
        hello = await session.wait_kind("core", "hello")
        if not shares:
            assert "camera_events" not in hello["modules"]
            hass.states.async_set("binary_sensor.front_door_person", "on", {"device_class": "motion"})
            await asyncio.sleep(0.2)
            assert session.kinds("camera_events", "event") == []
            return
        assert hello["modules"]["camera_events"] == 1
        listed = await session.wait_kind("camera_events", "list")
        assert listed["cameras"][0]["id"] == CAMERA and "person" in listed["cameras"][0]["kinds"]
        cameras = await session.wait_kind("cameras", "list")
        assert cameras["cameras"][0]["id"] == CAMERA
        hass.states.async_set("binary_sensor.front_door_person", "on", {"device_class": "motion"})
        event = await session.wait_kind("camera_events", "event")
        assert event["camera"] == CAMERA and event["kind"] == "person" and event["source"] == "binary_sensor.front_door_person"
        await session.send({"m": "camera_events", "t": "refresh"})
        await session.wait(lambda m: m["m"] == "camera_events" and m["t"] == "list" and len(session.kinds("camera_events", "list")) >= 2)
    finally:
        await fake.stop()
