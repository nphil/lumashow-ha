"""The LumaShow protocol's constants and codec (docs/protocol.md): one JSON object per message."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Final

PROTO: Final = 1
FRAME_JSON: Final = 1
MAX_MESSAGE: Final = 48 * 1024
MAX_PART: Final = 32 * 1024
LABEL: Final = b"LumaShow HA bridge v1"

NOISE_NAME: Final = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
NOISE_PROLOGUE: Final = b"NoiseAPIInit\x00\x00"

# The modules this integration implements, with the version it speaks of each.
MODULES: Final = {"states": 1, "actions": 1, "cameras": 1, "notify": 1}
# Names kept for later: no message is defined in version 1, and both ends ignore them.
RESERVED_MODULES: Final = ("alarms", "media", "presence", "intercom")

Message = dict[str, Any]


class ProtocolError(Exception):
    """Bytes that are not a message of the protocol."""


def channel_key(pairing_key: bytes) -> bytes:
    """The key of the channel, made from the Show's pairing key (the ESPHome device's), so that the two protocols never share one."""
    return hmac.new(pairing_key, LABEL, hashlib.sha256).digest()


def encode(message: Message) -> bytes:
    """One message as the bytes of a frame."""
    data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode()
    if len(data) > MAX_MESSAGE:
        raise ProtocolError(f"a message of {len(data)} bytes is longer than the protocol allows")
    return data


def decode(data: bytes) -> Message:
    """One message from the bytes of a frame: a JSON object with a module (``m``) and a kind (``t``)."""
    if len(data) > MAX_MESSAGE:
        raise ProtocolError("a message longer than the protocol allows")
    try:
        message = json.loads(data)
    except ValueError as err:
        raise ProtocolError("a message that is not JSON") from err
    if not isinstance(message, dict):
        raise ProtocolError("a message that is not an object")
    if not isinstance(message.get("m"), str) or not isinstance(message.get("t"), str):
        raise ProtocolError("a message with no module or no kind")
    return message


def message(module: str, kind: str, **fields: Any) -> Message:
    """A message of ``module`` and ``kind`` with these fields."""
    return {"m": module, "t": kind, **fields}
