"""Module ``camera_live``: a shared camera's live stream for a device with a video decoder (a LumaTV), without the camera's own address.

The device asks (``start``); the integration asks Home Assistant's stream integration for the camera's stream in HLS, which Home Assistant
pulls from the camera itself and serves as fragmented MP4 at a tokened address on its own port (``started``: the address; a device fetches
it from the home network and plays it). The camera's RTSP address and its password never leave Home Assistant; the stream stops by itself
once nothing fetches it (Home Assistant's own idle rule), and ``stop`` is the device saying it is done. Only a camera of the shared set.

Offered only when the person turned on *Send camera events* for that device (the same option as the events: a device that shows cameras
live is one that shows what they see), and only while the device registered a handler.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import OPT_CAMERA_EVENTS

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

MODULE: Final = "camera_live"
VERSION: Final = 1
FORMATS: Final = ("hls",)


class CameraLiveModule:
    """The owner of module ``camera_live`` (registered in ``hass.data`` for the hubs to offer and to call)."""

    version = VERSION

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._hubs: dict[str, Hub] = {}

    @staticmethod
    def sharing(hub: Hub) -> bool:
        return bool(hub.options.get(OPT_CAMERA_EVENTS))

    def offers(self, hub: Hub) -> bool:
        return self.sharing(hub)

    async def async_session(self, hub: Hub, up: bool) -> None:
        key = hub.entry.entry_id
        if up and self.sharing(hub):
            self._hubs[key] = hub
        else:
            self._hubs.pop(key, None)

    async def async_message(self, hub: Hub, kind: str, body: Mapping[str, Any]) -> None:
        if hub.entry.entry_id not in self._hubs:
            return
        if kind == "start":
            await self._start(hub, body)
        elif kind == "stop":
            _LOGGER.debug("%s: done with the stream of %s", hub.name, body.get("camera"))
        else:
            _LOGGER.debug("%s: a message camera_live.%s is not one this version knows", hub.name, kind)

    async def _start(self, hub: Hub, body: Mapping[str, Any]) -> None:
        op = body.get("op")
        op = op if isinstance(op, int) and not isinstance(op, bool) else 0
        camera = body.get("camera")
        fmt = body.get("format", "hls")

        async def refuse(code: str, message: str) -> None:
            _LOGGER.debug("%s: no stream for %s: %s (%s)", hub.name, camera, code, message)
            await hub.async_send_module(
                MODULE, "failed", {"op": op, "camera": camera if isinstance(camera, str) else "", "code": code, "message": message}
            )

        if not isinstance(camera, str) or not camera.startswith("camera."):
            return await refuse("bad_args", "camera names a camera entity")
        shared = hub.shared_entities().get(camera)
        if shared is None or shared.domain != "camera":
            return await refuse("not_allowed", f"{camera} is not shared with this device")
        if fmt not in FORMATS:
            return await refuse("bad_args", f"a {fmt} stream is not one this version makes")
        try:
            base = get_url(self.hass, allow_external=False, prefer_external=False, allow_cloud=False)
        except NoURLAvailableError:
            return await refuse("unavailable", "Home Assistant has no internal address")
        from homeassistant.components.camera import async_request_stream

        try:
            path = await async_request_stream(self.hass, camera, "hls")
        except HomeAssistantError as err:
            return await refuse("no_stream", str(err)[:200] or "the camera has no stream")
        except Exception as err:
            return await refuse("failed", str(err)[:200] or err.__class__.__name__)
        if not isinstance(path, str) or not path:
            return await refuse("no_stream", "the camera has no stream")
        await hub.async_send_module(MODULE, "started", {"op": op, "camera": camera, "kind": "hls", "url": base + path})
