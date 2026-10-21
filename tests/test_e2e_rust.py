"""The integration against the real Show side: the LumaShow repository's `ha-bridge` host example, a child process.

Skipped unless ``LUMASHOW_SHOW_BIN`` names that binary (``cargo build -p ha-bridge --example show`` in the LumaShow repository; it is
``target/debug/examples/show``). Nothing here is a fake of the Show: the Noise handshake, the framing, the negotiation, the mirror the Show
keeps, what it lets a plugin ask for and how it ends a session are all the Rust code that runs on a device.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.lumashow.channel import AuthError, Channel, probe
from custom_components.lumashow.const import DOMAIN, OPT_CONTROL_DOMAINS, OPT_ENTITIES, OPT_SENSITIVE

from .conftest import MAC, PAIRING_KEY, entry_data, setup
from .rig import with_pin

SHOW_BIN = os.environ.get("LUMASHOW_SHOW_BIN")

pytestmark = pytest.mark.skipif(not SHOW_BIN, reason="LUMASHOW_SHOW_BIN (the Show side's host example) is not set")


class RealShow:
    """The Show side as a child process: it prints what it is told as one JSON object a line, and reads commands on stdin."""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self.events: list[dict[str, Any]] = []
        self.port = 0
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()

    async def start(self) -> None:
        assert SHOW_BIN
        self._process = await asyncio.create_subprocess_exec(
            SHOW_BIN,
            "--key-hex",
            self.key.hex(),
            "--notices",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader = asyncio.create_task(self._read())
        self.port = (await self.wait(lambda e: e["event"] == "listening", 30))["port"]

    async def _read(self) -> None:
        assert self._process and self._process.stdout
        async for line in self._process.stdout:
            with contextlib.suppress(ValueError):
                self.events.append(json.loads(line))
                self._changed.set()

    async def wait(self, predicate: Any, timeout: float = 15.0) -> dict[str, Any]:
        """The first event (already seen or still to come) for which ``predicate`` holds."""
        seen = 0
        async with asyncio.timeout(timeout):
            while True:
                for event in self.events[seen:]:
                    if predicate(event):
                        return event
                seen = len(self.events)
                self._changed.clear()
                if seen != len(self.events):
                    continue
                await self._changed.wait()

    async def say(self, line: str) -> None:
        assert self._process and self._process.stdin
        self._process.stdin.write(f"{line}\n".encode())
        await self._process.stdin.drain()

    async def stop(self) -> None:
        if self._process is None:
            return
        with contextlib.suppress(Exception):
            await self.say("quit")
        try:
            await asyncio.wait_for(self._process.wait(), 10)
        except TimeoutError:
            self._process.kill()
            await self._process.wait()
        if self._reader:
            await self._reader
        if log := os.environ.get("LUMASHOW_E2E_LOG"):
            # What the real Show side printed, one JSON object a line: the proof of a run (the key is never in it).
            with open(log, "a", encoding="utf-8") as out:
                out.write(f"# {len(self.events)} events\n")
                out.writelines(json.dumps(event, ensure_ascii=False) + "\n" for event in self.events)


@pytest.fixture
async def real_show(socket_enabled: None) -> AsyncGenerator[RealShow]:
    show = RealShow(PAIRING_KEY)
    await show.start()
    yield show
    await show.stop()


def bridged(entities: int | None = None) -> Any:
    def check(event: dict[str, Any]) -> bool:
        if event["event"] != "status" or not event["status"].startswith("Bridged"):
            return False
        found = re.search(r"entities: (\d+)", event["status"])
        return entities is None or (found is not None and int(found.group(1)) == entities)

    return check


def entry_for(hass: HomeAssistant, show: RealShow, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Living Room Show", unique_id=MAC, data=entry_data(show.port), options=options)
    entry.add_to_hass(hass)
    return entry


async def test_the_real_show_side_and_the_integration_agree_end_to_end(hass: HomeAssistant, real_show: RealShow) -> None:
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen light", "brightness": 100})
    hass.states.async_set("sensor.hall", "21.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("lock.door", "locked", {"friendly_name": "Front door"})
    hass.states.async_set("switch.not_shared", "on")
    turn_on = async_mock_service(hass, "light", "turn_on")
    unlock = async_mock_service(hass, "lock", "unlock")
    entry = entry_for(
        hass,
        real_show,
        **{OPT_ENTITIES: ["light.kitchen", "sensor.hall", "lock.door"], OPT_CONTROL_DOMAINS: ["light", "lock"]},
        **with_pin(**{OPT_SENSITIVE: ["lock.door"]}),
    )
    hub = await setup(hass, entry)

    # 1. The Noise handshake, the hello and the welcome, and a snapshot that the Show's mirror took whole: three entities and no others.
    status = await real_show.wait(bridged(3))
    assert "controllable: 3" in status["status"] or "controllable: 2" in status["status"], status
    first = await real_show.wait(lambda e: e["event"] == "states" and e["initial"] and len(e["states"]) == 3)
    by_id = {s["id"]: s for s in first["states"]}
    assert sorted(by_id) == ["light.kitchen", "lock.door", "sensor.hall"], "exactly the list: the switch that is not on it was never sent"
    assert by_id["light.kitchen"]["state"] == "on" and by_id["light.kitchen"]["attrs"]["brightness"] == 100
    assert by_id["sensor.hall"]["attrs"]["unit_of_measurement"] == "°C"
    assert hub.connected and hub.agreed == {"states": 1, "actions": 1, "cameras": 1, "notify": 1}

    # 2. A change in Home Assistant reaches the Show's mirror.
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light", "brightness": 0})
    update = await real_show.wait(lambda e: e["event"] == "states" and not e["initial"] and any(s["id"] == "light.kitchen" for s in e["states"]))
    assert next(s for s in update["states"] if s["id"] == "light.kitchen")["state"] == "off"
    hass.states.async_set("switch.not_shared", "off")
    await asyncio.sleep(0.5)
    assert not any(s["id"] == "switch.not_shared" for e in real_show.events if e["event"] == "states" for s in e["states"])

    # 3. The Show asks for an action the integration allows: it is done in Home Assistant, and the Show is told so.
    await real_show.say('call light.kitchen turn_on {"brightness_pct": 40}')
    done = await real_show.wait(lambda e: e["event"] == "call" and e["verb"] == "turn_on")
    assert done["ok"] is True, done
    assert len(turn_on) == 1 and turn_on[0].data["entity_id"] == "light.kitchen" and turn_on[0].data["brightness_pct"] == 40
    assert hub.actions == 1

    # 4. Anything that is not one of the fixed actions of the entity's kind is refused, with words, and nothing is done.
    await real_show.say("call light.kitchen format_the_disk")
    refused = await real_show.wait(lambda e: e["event"] == "call" and e["verb"] == "format_the_disk")
    assert refused["ok"] is False and refused["error"], refused
    assert len(turn_on) == 1 and hub.actions == 1

    # 5. Unlocking is sensitive: no plugin on the Show has a PIN pad, so it is refused and the door stays as it is.
    await real_show.say("call lock.door unlock")
    sensitive = await real_show.wait(lambda e: e["event"] == "call" and e["verb"] == "unlock")
    assert sensitive["ok"] is False and sensitive["error"], sensitive
    assert not unlock and hub.actions == 1

    # 6. A message for the Show comes out as a notification.
    await hass.services.async_call(
        "notify", "send_message", {"entity_id": "notify.living_room_show_message", "message": "Dinner is ready", "title": "Kitchen"}, blocking=True
    )
    notices = await real_show.wait(lambda e: e["event"] == "notices" and any(n["message"] == "Dinner is ready" for n in e["notices"]))
    assert any(n["title"] == "Kitchen" for n in notices["notices"])

    # 7. The count the Show keeps is the one Home Assistant sees.
    await real_show.say("stats")
    stats = await real_show.wait(lambda e: e["event"] == "stats")
    assert stats["connected"] is True and stats["entities"] == 3 and stats["sessions"] == 1

    # 8. Unloading the entry: the Show is told, and drops everything at once.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await real_show.wait(lambda e: e["event"] == "status" and e["status"].startswith("Waiting"))
    await real_show.say("stats")
    gone = await real_show.wait(lambda e: e["event"] == "stats" and e is not stats)
    assert gone["connected"] is False and gone["entities"] == 0


async def test_a_key_the_show_does_not_hold_is_refused_at_the_handshake_and_counted(hass: HomeAssistant, real_show: RealShow) -> None:
    with pytest.raises(AuthError) as refused:
        await Channel.connect("127.0.0.1", real_show.port, bytes(range(1, 33)))
    assert refused.value.mac, "the Show still says what it is"
    name, mac = await probe("127.0.0.1", real_show.port)
    assert (name, mac) == ("lumashow-abcdef", "AA:BB:CC:DD:EE:FF")
    await real_show.say("stats")
    stats = await real_show.wait(lambda e: e["event"] == "stats")
    assert stats["connected"] is False and stats["entities"] == 0, "nothing was given to a connection that had no key"
