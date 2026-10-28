"""Module ``camera_live``: a shared camera's stream is asked of Home Assistant's stream integration and its address is answered; a camera
not shared, a format not made and a camera without a stream are refused in words; through the real hub and the fake Show."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumashow.camera_live import CameraLiveModule
from custom_components.lumashow.const import DOMAIN, OPT_CAMERA_EVENTS, OPT_ENTITIES
from custom_components.lumashow.selection import Shared

from .conftest import MAC, PAIRING_KEY, entry_data, setup
from .fake_show import FakeShow

CAMERA = "camera.front_door"


class FakeHub:
    def __init__(self, cameras: dict[str, str], *, sharing: bool = True) -> None:
        self.name = "Living Room TV"
        self.entry = SimpleNamespace(entry_id="entry-tv")
        self.options = {OPT_CAMERA_EVENTS: sharing}
        self.agreed = {"camera_live": 1}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._shared = {entity_id: Shared(entity_id=entity_id, name=name, area_id=None, minor=False) for entity_id, name in cameras.items()}

    def shared_entities(self) -> dict[str, Shared]:
        return self._shared

    async def async_send_module(self, module: str, kind: str, fields: Any) -> bool:
        assert module == "camera_live"
        self.sent.append((kind, dict(fields)))
        return True


async def test_a_start_is_answered_with_home_assistants_own_stream_address_and_refusals_are_in_words(hass: HomeAssistant) -> None:
    hass.config.internal_url = "http://192.0.2.10:8123"
    module = CameraLiveModule(hass)
    hub = FakeHub({CAMERA: "Front door"})
    await module.async_session(hub, True)  # type: ignore[arg-type]
    with patch("homeassistant.components.camera.async_request_stream", return_value="/api/hls/tok/master_playlist.m3u8") as request:
        await module.async_message(hub, "start", {"op": 7, "camera": CAMERA, "format": "hls"})  # type: ignore[arg-type]
    request.assert_awaited_once_with(hass, CAMERA, "hls")
    assert hub.sent == [("started", {"op": 7, "camera": CAMERA, "kind": "hls", "url": "http://192.0.2.10:8123/api/hls/tok/master_playlist.m3u8"})]
    hub.sent.clear()
    await module.async_message(hub, "start", {"op": 8, "camera": "camera.other"})  # type: ignore[arg-type]
    await module.async_message(hub, "start", {"op": 9, "camera": CAMERA, "format": "webrtc"})  # type: ignore[arg-type]
    await module.async_message(hub, "start", {"op": 10, "camera": "light.x"})  # type: ignore[arg-type]
    with patch("homeassistant.components.camera.async_request_stream", side_effect=HomeAssistantError("Camera does not support stream")):
        await module.async_message(hub, "start", {"op": 11, "camera": CAMERA})  # type: ignore[arg-type]
    assert [(k, f["op"], f["code"]) for k, f in hub.sent] == [
        ("failed", 8, "not_allowed"),
        ("failed", 9, "bad_args"),
        ("failed", 10, "bad_args"),
        ("failed", 11, "no_stream"),
    ]
    assert hub.sent[-1][1]["message"] == "Camera does not support stream"
    # a stop is taken quietly; after the session nothing is answered
    await module.async_message(hub, "stop", {"camera": CAMERA})  # type: ignore[arg-type]
    await module.async_session(hub, False)  # type: ignore[arg-type]
    await module.async_message(hub, "start", {"op": 12, "camera": CAMERA})  # type: ignore[arg-type]
    assert len(hub.sent) == 4


async def test_the_module_is_not_offered_without_camera_events(hass: HomeAssistant) -> None:
    module = CameraLiveModule(hass)
    assert module.offers(FakeHub({CAMERA: "Front door"}, sharing=False)) is False  # type: ignore[arg-type]
    assert module.offers(FakeHub({CAMERA: "Front door"})) is True  # type: ignore[arg-type]


async def test_a_device_asks_through_the_channel_and_is_answered(hass: HomeAssistant, socket_enabled: None) -> None:
    hass.config.internal_url = "http://192.0.2.10:8123"
    fake = FakeShow(PAIRING_KEY, modules={"states": 1, "cameras": 1, "camera_events": 1, "camera_live": 1})
    await fake.start()
    config = MockConfigEntry(
        domain=DOMAIN, title="Living Room TV", unique_id=MAC, data=entry_data(fake.port), options={OPT_ENTITIES: [CAMERA], OPT_CAMERA_EVENTS: True}
    )
    config.add_to_hass(hass)
    hass.states.async_set(CAMERA, "idle", {"friendly_name": "Front door"})
    try:
        await setup(hass, config)
        session = await fake.wait_session()
        hello = await session.wait_kind("core", "hello")
        assert hello["modules"]["camera_live"] == 1
        await session.wait_kind("cameras", "list")
        with patch("homeassistant.components.camera.async_request_stream", return_value="/api/hls/tok/master_playlist.m3u8"):
            await session.send({"m": "camera_live", "t": "start", "op": 3, "camera": CAMERA, "format": "hls"})
            started = await session.wait_kind("camera_live", "started")
        assert started["op"] == 3 and started["url"].endswith("/api/hls/tok/master_playlist.m3u8") and started["kind"] == "hls"
        await session.send({"m": "camera_live", "t": "stop", "camera": CAMERA})
    finally:
        await fake.stop()
