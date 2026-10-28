"""A fake Show: the Show's end of the protocol over a real socket, so that the integration is tested against the wire.

It is a Noise_NNpsk0 responder with ESPHome's framing, keyed with the channel key made from a pairing key, and it answers a ``hello`` with a
``welcome``. A test reads what the integration sent (``received``) and sends what the Show would (``send``).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from cryptography.exceptions import InvalidTag
from noise.connection import NoiseConnection

from custom_components.lumashow.protocol import (
    FRAME_JSON,
    NOISE_NAME,
    NOISE_PROLOGUE,
    PROTO,
    channel_key,
    decode,
    encode,
)

DEVICE = {
    "name": "lumashow-abcdef",
    "friendly_name": "LumaShow abcdef",
    "mac": "AA:BB:CC:DD:EE:FF",
    "manufacturer": "LumaShow",
    "model": "Echo Show 5",
    "sw_version": "v1-test",
    "display": {"width": 960, "height": 480},
    "has": {"camera": True, "microphone": True, "speaker": True},
}


def _frame(body: bytes) -> bytes:
    return b"\x01" + len(body).to_bytes(2, "big") + body


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readexactly(3)
    return await reader.readexactly(int.from_bytes(head[1:], "big"))


class FakeSession:
    """One connection of the integration, as the Show sees it."""

    def __init__(self, show: FakeShow, writer: asyncio.StreamWriter, noise: NoiseConnection) -> None:
        self.show = show
        self.writer = writer
        self.noise = noise
        self.received: list[dict[str, Any]] = []
        self.binary: list[tuple[int, bytes]] = []
        self.closed = asyncio.Event()
        self._changed = asyncio.Event()

    async def send(self, message: dict[str, Any]) -> None:
        payload = encode(message)
        plain = FRAME_JSON.to_bytes(2, "big") + len(payload).to_bytes(2, "big") + payload
        self.writer.write(_frame(self.noise.encrypt(plain)))
        await self.writer.drain()

    def kinds(self, module: str, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.received if m["m"] == module and m["t"] == kind]

    async def wait(self, predicate: Any, timeout: float = 3.0) -> dict[str, Any]:
        """The first message received (now or later) for which ``predicate`` holds."""
        seen = 0
        async with asyncio.timeout(timeout):
            while True:
                for message in self.received[seen:]:
                    if predicate(message):
                        return message
                seen = len(self.received)
                self._changed.clear()
                if seen != len(self.received):
                    continue
                await self._changed.wait()

    async def wait_kind(self, module: str, kind: str, timeout: float = 3.0) -> dict[str, Any]:
        return await self.wait(lambda m: m["m"] == module and m["t"] == kind, timeout)

    async def wait_binary(self, count: int, timeout: float = 5.0) -> list[tuple[int, bytes]]:
        """At least ``count`` binary frames received (now or later)."""
        async with asyncio.timeout(timeout):
            while len(self.binary) < count:
                self._changed.clear()
                if len(self.binary) >= count:
                    break
                await self._changed.wait()
        return list(self.binary)

    async def wait_closed(self, timeout: float = 3.0) -> None:
        async with asyncio.timeout(timeout):
            await self.closed.wait()

    def _got(self, message: dict[str, Any]) -> None:
        self.received.append(message)
        self._changed.set()


class FakeShow:
    """Listens on a free local port and plays the Show."""

    def __init__(
        self,
        pairing_key: bytes,
        *,
        device: dict[str, Any] | None = None,
        modules: dict[str, int] | None = None,
        answer_hello: bool = True,
    ) -> None:
        self.key = channel_key(pairing_key)
        self.device = dict(DEVICE if device is None else device)
        self.modules = {"states": 1, "actions": 1, "cameras": 1, "notify": 1} if modules is None else modules
        self.answer_hello = answer_hello
        self.sessions: list[FakeSession] = []
        self.refused = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # A connection that was being accepted when the server closed is still made a step or two later, in the middle of `stop()` (the integration
        # reconnecting as a test ends): it is refused, and nothing is served that `stop()` would not see. (A coroutine callback would also be started
        # by the stream protocol a step after the connection is made, so that its task was not in `_tasks` yet when `stop()` looked.)
        if self._stopping:
            writer.close()
            return
        task = asyncio.get_running_loop().create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        # No new connection from here on, then the open ones are closed: Python 3.12+ waits for every connection in `wait_closed`, whoever still holds them.
        self._stopping = True
        if self._server:
            self._server.close()
        for session in self.sessions:
            session.writer.close()
        while self._tasks:
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._server:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2):
                    await self._server.wait_closed()

    @property
    def session(self) -> FakeSession:
        return self.sessions[-1]

    async def wait_session(self, timeout: float = 5.0, *, count: int = 1) -> FakeSession:
        async with asyncio.timeout(timeout):
            while len(self.sessions) < count:
                await asyncio.sleep(0.01)
        return self.sessions[count - 1]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _read_frame(reader)  # the client's (empty) hello
        first = await _read_frame(reader)  # 0x00 and the first handshake message
        noise = NoiseConnection.from_name(NOISE_NAME)
        noise.set_as_responder()
        noise.set_psks(self.key)
        noise.set_prologue(NOISE_PROLOGUE)
        noise.start_handshake()
        name, mac = self.device["name"], self.device["mac"]
        writer.write(_frame(b"\x01" + name.encode() + b"\x00" + mac.encode() + b"\x00"))
        try:
            noise.read_message(first[1:])
        except InvalidTag:
            self.refused += 1
            writer.write(_frame(b"\x01Handshake MAC failure"))
            await writer.drain()
            return
        writer.write(_frame(b"\x00" + noise.write_message()))
        await writer.drain()
        session = FakeSession(self, writer, noise)
        self.sessions.append(session)
        try:
            while True:
                frame = await _read_frame(reader)
                plain = noise.decrypt(frame)
                if int.from_bytes(plain[:2], "big") != FRAME_JSON:
                    session.binary.append((int.from_bytes(plain[:2], "big"), plain[4 : 4 + int.from_bytes(plain[2:4], "big")]))
                    session._changed.set()
                    continue
                message = decode(plain[4 : 4 + int.from_bytes(plain[2:4], "big")])
                session._got(message)
                if message["m"] == "core" and message["t"] == "hello" and self.answer_hello:
                    offered = message.get("modules") or {}
                    agreed = {n: min(v, offered[n]) for n, v in self.modules.items() if n in offered}
                    await session.send(
                        {
                            "m": "core",
                            "t": "welcome",
                            "proto": PROTO,
                            "device": self.device,
                            "modules": agreed,
                            "limits": {"message_bytes": 49152, "entities": 256, "call_entities": 16},
                        }
                    )
                elif message["m"] == "core" and message["t"] == "ping":
                    await session.send({"m": "core", "t": "pong", "n": message.get("n", 0)})
        finally:
            session.closed.set()
            session._changed.set()
