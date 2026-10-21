"""The PIN that sensitive actions need: kept only as a hash, in Home Assistant, and checked here (the Show never holds it).

scrypt with the entry's own salt; the comparison takes the same time whatever the digits are. The checks are CPU-heavy by design, so callers
run them in the executor.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Final

from .const import OPT_PIN_HASH, OPT_PIN_SALT, PIN_MAX_LEN, PIN_MIN_LEN

_N: Final = 2**14


def hash_pin(pin: str, salt: bytes) -> str:
    """The PIN as it is kept (hex)."""
    return hashlib.scrypt(pin.encode(), salt=salt, n=_N, r=8, p=1, dklen=32).hex()


def new_pin(pin: str) -> tuple[str, str]:
    """``(hash, salt)`` of a new PIN, both as hex, with a salt of its own."""
    salt = os.urandom(16)
    return hash_pin(pin, salt), salt.hex()


def pin_ok(pin: str, salt_hex: str, expected_hex: str) -> bool:
    """Whether ``pin`` is the PIN that ``expected_hex`` was made from."""
    return hmac.compare_digest(hash_pin(pin, bytes.fromhex(salt_hex)), expected_hex)


def matches(pin: str, options: dict[str, Any]) -> bool:
    """Whether ``pin`` is the PIN the options hold."""
    salt, expected = options.get(OPT_PIN_SALT), options.get(OPT_PIN_HASH)
    return bool(salt and expected) and pin_ok(pin, salt, expected)


def well_formed(pin: str) -> bool:
    """Four to eight digits."""
    return pin.isdigit() and pin.isascii() and PIN_MIN_LEN <= len(pin) <= PIN_MAX_LEN
