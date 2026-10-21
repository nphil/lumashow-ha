"""What several test files use: a Show connected to an entry that shares what a test chooses, and the calls the Show would make."""

from __future__ import annotations

from itertools import count
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_mock_service,
)

from custom_components.lumashow import hub as hub_module
from custom_components.lumashow.const import (
    EVENT_ACTION,
    OPT_CONTROL_DOMAINS,
    OPT_ENTITIES,
    OPT_PIN_HASH,
    OPT_PIN_SALT,
)
from custom_components.lumashow.pin import new_pin

from .conftest import setup
from .fake_show import FakeSession, FakeShow

PIN = "2468"
ops = count(1)


async def ask(session: FakeSession, entity_ids: Any, verb: Any, args: Any = None, **extra: Any) -> dict[str, Any]:
    """Sends an ``actions.call`` as the Show would and returns the one result."""
    op = next(ops)
    call: dict[str, Any] = {"m": "actions", "t": "call", "op": op, "entity_ids": entity_ids, "verb": verb}
    if args is not None:
        call["args"] = args
    call.update(extra)
    await session.send(call)
    return await session.wait(lambda m: m["m"] == "actions" and m["t"] == "result" and m["op"] == op)


class Rig:
    """An entry set up with what a test shares, the Show connected, and the services a test looks at mocked."""

    def __init__(self, hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> None:
        self.hass, self.show, self.entry = hass, show, entry
        self.logbook = async_mock_service(hass, "logbook", "log")
        self.events = async_capture_events(hass, EVENT_ACTION)
        self.session: FakeSession
        self.hub: hub_module.Hub

    async def start(self, entities: list[str], control: list[str] | None = None, **options: Any) -> Rig:
        options = {OPT_ENTITIES: entities, OPT_CONTROL_DOMAINS: control or [], **options}
        self.hass.config_entries.async_update_entry(self.entry, options=options)
        self.hub = await setup(self.hass, self.entry)
        self.session = await self.show.wait_session()
        await self.session.wait(lambda m: m["m"] == "states" and m["t"] == "snapshot")
        return self


def with_pin(**options: Any) -> dict[str, Any]:
    hashed, salt = new_pin(PIN)
    return {OPT_PIN_HASH: hashed, OPT_PIN_SALT: salt, **options}


def light(hass: HomeAssistant, entity_id: str = "light.a", name: str = "Kitchen light") -> None:
    hass.states.async_set(entity_id, "on", {"friendly_name": name})
