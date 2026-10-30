"""Module ``camera_live``: a shared camera's live stream for a device with a video decoder (a LumaTV), without the camera's own address.

Two ways, the device saying which it can take (``start.formats``, preferred first), the integration giving the first it can:

- **relay** (low latency): Home Assistant's own go2rtc restreams the camera over RTSP on the loopback; this module reads that restream inside
  Home Assistant (``rtsp.py``: RTP over TCP, the H.264 or H.265 access units put back together) and relays the access units to the device
  over the paired channel as binary frames (type ``FRAME_RELAY``), whole and in order, with the RTP timestamp. Nothing is opened on Home
  Assistant, the camera's address and password stay inside, and the cost is copying packets. The device feeds them to its decoder as they
  arrive: the latency is the camera's and the decoder's, not a segment's.
- **hls**: Home Assistant's stream integration serves the camera as low-latency HLS at a tokened address (``started.url``); the device
  fetches and plays it. A few seconds behind, no go2rtc needed.

Offered only when the person turned on *Send camera events* for that device (a device that shows cameras live is one that shows what they
see), only while the device registered a handler, and only for cameras of the shared set. One relay per device at a time; ``stop`` ends it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.network import NoURLAvailableError, get_url

from . import rtsp
from .const import OPT_CAMERA_EVENTS

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

MODULE: Final = "camera_live"
VERSION: Final = 1
FORMATS: Final = ("relay", "hls")
# The binary frame type of the relay (protocol §1: type 1 is a message, the others are binary modules').
FRAME_RELAY: Final = 2
# A relay chunk: stream (2), flags (1), reserved (1), sequence (4), RTP timestamp (4), then at most CHUNK bytes of the access unit.
HEADER: Final = 12
CHUNK: Final = 60000
FLAG_FIRST: Final = 1
FLAG_LAST: Final = 2
FLAG_KEY: Final = 4
# go2rtc as Home Assistant runs it: its RTSP restream on the loopback.
GO2RTC_RTSP: Final = "rtsp://127.0.0.1:18554/{identifier}"


class CameraLiveModule:
    """The owner of module ``camera_live`` (registered in ``hass.data`` for the hubs to offer and to call)."""

    version = VERSION

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._hubs: dict[str, Hub] = {}
        self._relays: dict[str, Relay] = {}
        self._streams = 0

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
            await self._end_relay(key, "the session ended")

    async def async_message(self, hub: Hub, kind: str, body: Mapping[str, Any]) -> None:
        if hub.entry.entry_id not in self._hubs:
            return
        if kind == "start":
            await self._start(hub, body)
        elif kind == "stop":
            await self._end_relay(hub.entry.entry_id, "the device is done")
        else:
            _LOGGER.debug("%s: a message camera_live.%s is not one this version knows", hub.name, kind)

    # -- start ------------------------------------------------------------------------------------------------------------------------

    async def _start(self, hub: Hub, body: Mapping[str, Any]) -> None:
        op = body.get("op")
        op = op if isinstance(op, int) and not isinstance(op, bool) else 0
        camera = body.get("camera")
        wanted = body.get("formats")
        if not isinstance(wanted, list) or not all(isinstance(f, str) for f in wanted):
            single = body.get("format", "hls")
            wanted = [single] if isinstance(single, str) else ["hls"]

        async def refuse(code: str, message: str) -> None:
            _LOGGER.debug("%s: no stream for %s: %s (%s)", hub.name, camera, code, message)
            named = camera if isinstance(camera, str) else ""
            await hub.async_send_module(MODULE, "failed", {"op": op, "camera": named, "code": code, "message": message})

        if not isinstance(camera, str) or not camera.startswith("camera."):
            return await refuse("bad_args", "camera names a camera entity")
        shared = hub.shared_entities().get(camera)
        if shared is None or shared.domain != "camera":
            return await refuse("not_allowed", f"{camera} is not shared with this device")
        formats = [f for f in wanted if f in FORMATS]
        if not formats:
            return await refuse("bad_args", f"none of {wanted} is a stream this version makes")
        await self._end_relay(hub.entry.entry_id, "another stream was asked for")
        reasons: list[str] = []
        for fmt in formats:
            if fmt == "relay":
                why = await self._start_relay(hub, op, camera)
            else:
                why = await self._start_hls(hub, op, camera)
            if why is None:
                return None
            reasons.append(f"{fmt}: {why}" if len(formats) > 1 else why)
        return await refuse("no_stream", "; ".join(reasons)[:200])

    async def _start_hls(self, hub: Hub, op: int, camera: str) -> str | None:
        try:
            base = get_url(self.hass, allow_external=False, prefer_external=False, allow_cloud=False)
        except NoURLAvailableError:
            return "Home Assistant has no internal address"
        from homeassistant.components.camera import async_request_stream

        try:
            path = await async_request_stream(self.hass, camera, "hls")
        except HomeAssistantError as err:
            return str(err)[:120] or "the camera has no stream"
        except Exception as err:  # whatever the camera's integration raises is the answer, not a crash of the channel
            return str(err)[:120] or err.__class__.__name__
        if not isinstance(path, str) or not path:
            return "the camera has no stream"
        await hub.async_send_module(MODULE, "started", {"op": op, "camera": camera, "kind": "hls", "url": base + path})
        return None

    async def _start_relay(self, hub: Hub, op: int, camera: str) -> str | None:
        try:
            url = await self._relay_source(camera)
        except HomeAssistantError as err:
            return str(err)[:120]
        except Exception as err:  # go2rtc missing, the camera's integration failing: the words are the answer
            return (str(err) or err.__class__.__name__)[:120]
        if not url:
            return "Home Assistant's own go2rtc is not running"
        try:
            session = await rtsp.open(url)
        except rtsp.RtspError as err:
            return str(err)[:120]
        self._streams = self._streams % 65535 + 1
        relay = Relay(hub, session, self._streams, camera)
        self._relays[hub.entry.entry_id] = relay
        sets = session.track.parameter_sets
        await hub.async_send_module(
            MODULE,
            "started",
            {
                "op": op,
                "camera": camera,
                "kind": "relay",
                "stream": relay.stream,
                "codec": session.track.codec,
                "clock": session.track.clock,
                "parameter_sets": base64.b64encode(b"".join(rtsp.START_CODE + s for s in sets)).decode() if sets else "",
            },
        )
        relay.task = hub.entry.async_create_background_task(self.hass, relay.run(self), f"lumashow camera relay {camera}")
        return None

    async def _relay_source(self, camera: str) -> str | None:
        """go2rtc's RTSP restream of the camera, as Home Assistant itself registers it: ``None`` when Home Assistant's own go2rtc is not there."""
        # Home Assistant keeps its go2rtc's address and session under HassKey("go2rtc") (homeassistant/components/go2rtc, 2024.11 on)
        config = self.hass.data.get("go2rtc")
        if config is None:
            return None
        from homeassistant.components.camera.const import DATA_COMPONENT
        from homeassistant.components.go2rtc.const import HA_MANAGED_URL
        from homeassistant.components.go2rtc.util import get_camera_identifier

        if getattr(config, "url", None) != HA_MANAGED_URL:
            return None
        component = self.hass.data.get(DATA_COMPONENT)
        entity = component.get_entity(camera) if component is not None else None
        if entity is None:
            raise HomeAssistantError(f"{camera} is not a camera entity")
        source = await entity.stream_source()
        if not source:
            raise HomeAssistantError("the camera has no stream source")
        from go2rtc_client import Go2RtcRestClient

        client = Go2RtcRestClient(config.session, config.url)
        identifier = get_camera_identifier(entity)
        streams = await client.streams.list()
        stream = streams.get(identifier)
        if stream is None or not any(source == producer.url for producer in stream.producers):
            await client.streams.add(identifier, [source])
        return GO2RTC_RTSP.format(identifier=identifier)

    async def _end_relay(self, key: str, why: str) -> None:
        relay = self._relays.pop(key, None)
        if relay is not None:
            await relay.end(why)

    def relay_of(self, hub: Hub) -> Relay | None:
        """For tests and diagnostics."""
        return self._relays.get(hub.entry.entry_id)


class Relay:
    """One device's relay: the restream read, the access units chunked onto the channel."""

    def __init__(self, hub: Hub, session: rtsp.Session, stream: int, camera: str) -> None:
        self.hub = hub
        self.session = session
        self.stream = stream
        self.camera = camera
        self.task: asyncio.Task[None] | None = None
        self.units = 0
        self.frames = 0
        self._seq = 0
        self._ended = False

    async def run(self, owner: CameraLiveModule) -> None:
        why = "the stream ended"
        try:
            async for unit in self.session.units():
                if not await self._send(unit):
                    why = "the device went"
                    return
        except rtsp.RtspError as err:
            why = str(err)
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await self.session.close()
            if not self._ended:
                self._ended = True
                owner._relays.pop(self.hub.entry.entry_id, None)  # the owner's own bookkeeping
                self.hub.spawn_module_send(MODULE, "ended", {"camera": self.camera, "stream": self.stream, "reason": why})

    async def _send(self, unit: rtsp.AccessUnit) -> bool:
        data = unit.annex_b()
        # a key picture carries its parameter sets; the device starts from one
        if unit.key and not rtsp.parameter_sets_of(self.session.track.codec, unit.nals) and self.session.track.parameter_sets:
            data = b"".join(rtsp.START_CODE + s for s in self.session.track.parameter_sets) + data
        pieces = [data[i : i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
        for index, piece in enumerate(pieces):
            flags = (FLAG_FIRST if index == 0 else 0) | (FLAG_LAST if index == len(pieces) - 1 else 0) | (FLAG_KEY if unit.key else 0)
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            timestamp = (unit.timestamp & 0xFFFFFFFF).to_bytes(4, "big")
            header = self.stream.to_bytes(2, "big") + bytes([flags, 0]) + self._seq.to_bytes(4, "big") + timestamp
            if not await self.hub.async_send_binary(FRAME_RELAY, header + piece):
                return False
            self.frames += 1
        self.units += 1
        return True

    async def end(self, why: str) -> None:
        self._ended = True
        # the reading task first: the teardown reads the answer, and two readers of one socket cannot
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        with contextlib.suppress(Exception):
            await self.session.close()
        _LOGGER.debug("%s: relay of %s ended: %s (%d units)", self.hub.name, self.camera, why, self.units)
