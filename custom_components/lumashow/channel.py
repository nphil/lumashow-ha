"""The encrypted channel to a Show: Noise_NNpsk0 over ESPHome's framing, carrying the protocol's JSON messages.

The Show listens and this integration dials. The handshake and the frames are those of ESPHome's encrypted API (the Show's ``esphome-api``
serves both), keyed with the channel key made from the Show's pairing key (``protocol.channel_key``): the key Home Assistant already holds
for the ESPHome device of the same Show, so nothing is typed twice.

A frame is ``0x01``, a length (two bytes, big endian) and that many bytes; after the handshake those bytes are one Noise message whose
plain text is a type (two bytes), a length (two bytes) and the payload. Type 1 is a protocol message (a JSON object); any other type is
reserved and skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Final

from cryptography.exceptions import InvalidTag
from noise.connection import NoiseConnection

from .protocol import (
    FRAME_JSON,
    MAX_MESSAGE,
    NOISE_NAME,
    NOISE_PROLOGUE,
    Message,
    ProtocolError,
    channel_key,
    decode,
    encode,
)

PREAMBLE: Final = 0x01
# The transport's frame length is two bytes; a Noise message adds 16 bytes of tag and the plaintext four of header.
MAX_FRAME_PAYLOAD: Final = 65535 - 16 - 4
PLAIN_PREAMBLE: Final = 0x00
HELLO_FRAME: Final = b"\x01\x00\x00"
HANDSHAKE_MAC_FAILURE: Final = "Handshake MAC failure"


class ChannelError(Exception):
    """The channel could not be made or broke."""


class CannotConnect(ChannelError):
    """The Show could not be reached."""


class AuthError(ChannelError):
    """The Show did not accept the key (or is not a Show that holds this one). It still said what it is: its name and address."""

    def __init__(self, message: str, *, name: str = "", mac: str = "") -> None:
        super().__init__(message)
        self.name = name
        self.mac = mac


def _frame(body: bytes) -> bytes:
    return bytes((PREAMBLE,)) + len(body).to_bytes(2, "big") + body


def normalise_mac(mac: str) -> str:
    """A hardware address as twelve lower-case hex digits, however it was written."""
    return "".join(c for c in mac.lower() if c in "0123456789abcdef")


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    preamble = (await reader.readexactly(1))[0]
    if preamble == PLAIN_PREAMBLE:
        raise ChannelError("the Show speaks the plain protocol: this is not the LumaShow channel")
    if preamble != PREAMBLE:
        raise ChannelError(f"a frame with a wrong first byte ({preamble})")
    length = int.from_bytes(await reader.readexactly(2), "big")
    return await reader.readexactly(length)


class Channel:
    """One open, encrypted connection to a Show."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        noise: NoiseConnection,
        name: str,
        mac: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._noise = noise
        self._write_lock = asyncio.Lock()
        self.name = name
        self.mac = mac
        self.closed = False

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        pairing_key: bytes,
        *,
        timeout: float = 10.0,
        expected_mac: str | None = None,
    ) -> Channel:
        """Connects and makes the handshake. Raises ``CannotConnect`` or ``AuthError`` (a key the Show does not hold) or ``ChannelError``."""
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, TimeoutError) as err:
            raise CannotConnect(f"{host}:{port} cannot be reached: {err}") from err
        try:
            return await asyncio.wait_for(cls._handshake(reader, writer, pairing_key, expected_mac), timeout)
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

    @classmethod
    async def _handshake(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        pairing_key: bytes,
        expected_mac: str | None,
    ) -> Channel:
        noise = NoiseConnection.from_name(NOISE_NAME)
        noise.set_as_initiator()
        noise.set_psks(channel_key(pairing_key))
        noise.set_prologue(NOISE_PROLOGUE)
        noise.start_handshake()
        first = noise.write_message()
        writer.write(HELLO_FRAME + _frame(b"\x00" + first))
        try:
            await writer.drain()
            hello = await _read_frame(reader)
            reply = await _read_frame(reader)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as err:
            raise ChannelError(f"the Show closed the connection during the handshake: {err!r}") from err
        if not hello or hello[0] != 0x01:
            raise ChannelError("the Show's hello is not one this integration knows")
        parts = hello[1:].split(b"\x00")
        name = parts[0].decode(errors="replace")[:64]
        mac = parts[1].decode(errors="replace")[:32] if len(parts) > 1 else ""
        if expected_mac and mac and normalise_mac(mac) != normalise_mac(expected_mac):
            raise ChannelError(f"this is {mac}, not the Show with address {expected_mac}")
        if not reply:
            raise ChannelError("the Show's handshake frame is empty")
        if reply[0] != 0:
            reason = reply[1:].decode(errors="replace")
            if reason == HANDSHAKE_MAC_FAILURE:
                raise AuthError("the Show does not hold this key", name=name, mac=mac)
            raise ChannelError(f"the Show refused the handshake: {reason}")
        try:
            noise.read_message(reply[1:])
        except InvalidTag as err:
            raise AuthError("the Show does not hold this key", name=name, mac=mac) from err
        except Exception as err:
            raise ChannelError(f"the handshake failed: {err}") from err
        if not noise.handshake_finished:
            raise ChannelError("the handshake did not finish")
        return cls(reader, writer, noise, name, mac)

    async def send(self, message: Message) -> None:
        """Sends one message. Raises ``ChannelError`` when the connection is gone, ``ProtocolError`` for a message too long."""
        payload = encode(message)
        await self.send_frame(FRAME_JSON, payload)

    async def send_frame(self, kind: int, payload: bytes) -> None:
        """Sends one frame of ``kind`` (the message type, or a binary module's): at most ``MAX_FRAME_PAYLOAD`` bytes."""
        if kind != FRAME_JSON and len(payload) > MAX_FRAME_PAYLOAD:
            raise ProtocolError("a binary frame longer than the transport allows")
        plain = kind.to_bytes(2, "big") + len(payload).to_bytes(2, "big") + payload
        async with self._write_lock:
            if self.closed:
                raise ChannelError("the channel is closed")
            try:
                self._writer.write(_frame(self._noise.encrypt(plain)))
                await self._writer.drain()
            except (ConnectionError, OSError) as err:
                raise ChannelError(f"the connection broke: {err!r}") from err

    async def receive(self) -> Message | None:
        """The next message; ``None`` when the Show closed the connection. Raises ``ChannelError`` / ``ProtocolError`` for what is not one."""
        while True:
            try:
                frame = await _read_frame(self._reader)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return None
            try:
                plain = self._noise.decrypt(frame)
            except InvalidTag as err:
                raise ChannelError("a frame that does not decrypt") from err
            if len(plain) < 4:
                raise ChannelError("a decrypted frame that is too short")
            kind = int.from_bytes(plain[:2], "big")
            if kind != FRAME_JSON:
                continue
            payload = plain[4 : 4 + int.from_bytes(plain[2:4], "big")]
            if len(payload) > MAX_MESSAGE:
                raise ProtocolError("a message longer than the protocol allows")
            return decode(payload)

    async def close(self) -> None:
        """Closes the connection (what is written has been)."""
        self.closed = True
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


async def probe(host: str, port: int, *, timeout: float = 10.0) -> tuple[str, str]:
    """What a Show at this address says it is, ``(name, mac)``, without holding its key.

    A connection made with a key that is no key is refused at the handshake, but the Show has said its name and its address by then: that is all
    this learns, so the flow can look for the Show's ESPHome entry (its key) before it asks the person for anything.
    """
    try:
        channel = await Channel.connect(host, port, bytes(32), timeout=timeout)
    except AuthError as err:
        if not err.mac:
            raise ChannelError("the Show did not say what it is") from err
        return err.name, err.mac
    await channel.close()
    raise ChannelError("this Show accepted a key that is no key")
