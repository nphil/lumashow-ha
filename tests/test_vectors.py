"""The protocol's vectors, shared with the Show side (crates/ha-bridge/tests/vectors.json in the LumaShow repository).

Both sides decode every message here, the Show side writes each one back as the same object, and the key made from a pairing key is the same
bytes on both. What this integration sends is held to the vectors' fields: it says nothing the Show's side has no word for.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.lumashow.protocol import LABEL, MAX_MESSAGE, ProtocolError, channel_key, decode, encode

from .rig import Rig, ask, light, with_pin
from .test_hub import snapshot_of

VECTORS = json.loads((Path(__file__).parent / "vectors" / "protocol.json").read_text(encoding="utf-8"))


def test_the_channel_keys_are_the_vectors() -> None:
    assert len(VECTORS["channel_keys"]) >= 2
    for vector in VECTORS["channel_keys"]:
        pairing = bytes.fromhex(vector["pairing_key_hex"])
        assert channel_key(pairing).hex() == vector["channel_key_hex"]
        assert hmac.new(pairing, LABEL, hashlib.sha256).hexdigest() == vector["channel_key_hex"]
        assert channel_key(pairing) != pairing, "the ESPHome device's key itself is never the channel's"


@pytest.mark.parametrize("vector", VECTORS["messages"], ids=lambda v: v["name"])
def test_a_message_decodes_and_is_written_back_as_the_same_object(vector: dict[str, Any]) -> None:
    wanted = vector["message"]
    message = decode(json.dumps(wanted).encode())
    assert message == wanted
    assert (message["m"], message["t"]) == (wanted["m"], wanted["t"])
    assert json.loads(encode(message)) == wanted


@pytest.mark.parametrize("vector", VECTORS["tolerated"], ids=lambda v: v["name"])
def test_what_nobody_knows_yet_is_not_an_error(vector: dict[str, Any]) -> None:
    message = decode(vector["text"].encode())
    assert (message["m"], message["t"]) == (vector["module"], vector["kind"])


@pytest.mark.parametrize("vector", VECTORS["refused"], ids=lambda v: v["name"])
def test_what_is_not_a_message_is_refused(vector: dict[str, Any]) -> None:
    with pytest.raises(ProtocolError):
        decode(vector["text"].encode())


def test_a_message_that_is_too_long_is_refused_both_ways() -> None:
    long = {"m": "x", "t": "y", "pad": "a" * MAX_MESSAGE}
    with pytest.raises(ProtocolError):
        encode(long)
    with pytest.raises(ProtocolError):
        decode(json.dumps(long).encode())


def fields_of(module: str, kind: str) -> dict[str, set[str]]:
    """Every field the vectors give a message of this kind, at its top and in the lists of records it holds."""
    found: dict[str, set[str]] = {"": set()}
    for vector in VECTORS["messages"]:
        message = vector["message"]
        if (message["m"], message["t"]) != (module, kind):
            continue
        found[""] |= set(message)
        for key, value in message.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                for record in value:
                    found.setdefault(key, set()).update(record)
    return found


def held_to(message: dict[str, Any], module: str, kind: str) -> None:
    allowed = fields_of(module, kind)
    assert set(message) <= allowed[""], f"{module}.{kind} says {set(message) - allowed['']}, which the Show side has no word for"
    for key, records in message.items():
        if isinstance(records, list) and records and isinstance(records[0], dict):
            for record in records:
                assert set(record) <= allowed.get(key, set()), f"{module}.{kind} {key}: {set(record) - allowed.get(key, set())}"


async def test_what_this_integration_sends_uses_only_the_fields_of_the_vectors(hass: HomeAssistant, rig: Rig) -> None:
    light(hass)
    hass.states.async_set("lock.door", "locked", {"friendly_name": "Front door"})
    hass.states.async_set("sensor.hall", "21", {"unit_of_measurement": "°C"})
    await rig.start(["light.a", "lock.door", "sensor.hall"], ["light", "lock"], **with_pin(sensitive=["lock.door"]))
    held_to(await snapshot_of(rig.session), "states", "snapshot")
    hello = await rig.session.wait_kind("core", "hello")
    held_to(hello, "core", "hello")
    hass.states.async_set("light.a", "off", {"friendly_name": "Kitchen light"})
    update = await rig.session.wait_kind("states", "update")
    held_to(update, "states", "update")
    # The answers: one that was done, and one that was refused.
    async_mock_service(hass, "light", "turn_on")
    done = await ask(rig.session, ["light.a"], "turn_on")
    held_to(done, "actions", "result")
    refused = await ask(rig.session, ["light.a"], "format_the_disk")
    held_to(refused, "actions", "result")
    await hass.services.async_call("notify", "send_message", {"entity_id": "notify.living_room_show_message", "message": "Hi"}, blocking=True)
    held_to(await rig.session.wait_kind("notify", "post"), "notify", "post")
