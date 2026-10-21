"""The channel to a Show: the handshake, the framing and what a wrong key looks like."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.lumashow.channel import AuthError, CannotConnect, Channel, ChannelError, probe
from custom_components.lumashow.protocol import MAX_MESSAGE, ProtocolError, channel_key, decode, encode, message

from .conftest import PAIRING_KEY
from .fake_show import FakeShow

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def test_a_channel_with_the_right_key_carries_messages_both_ways(show: FakeShow) -> None:
    channel = await Channel.connect("127.0.0.1", show.port, PAIRING_KEY, expected_mac="AA:BB:CC:DD:EE:FF")
    assert channel.name == "lumashow-abcdef"
    await channel.send(message("core", "hello", proto=[1], modules={"states": 1}))
    session = await show.wait_session()
    welcome = await channel.receive()
    assert welcome is not None and welcome["t"] == "welcome" and welcome["modules"] == {"states": 1}
    await session.send(message("states", "snapshot", rev=1, entities=[]))
    snapshot = await channel.receive()
    assert snapshot == message("states", "snapshot", rev=1, entities=[])
    await channel.close()
    await session.wait_closed()


async def test_a_wrong_key_is_refused_and_the_show_still_says_who_it_is(show: FakeShow) -> None:
    with pytest.raises(AuthError) as refused:
        await Channel.connect("127.0.0.1", show.port, bytes(32))
    assert refused.value.mac == "AA:BB:CC:DD:EE:FF"
    assert refused.value.name == "lumashow-abcdef"
    assert show.refused == 1
    assert await probe("127.0.0.1", show.port) == ("lumashow-abcdef", "AA:BB:CC:DD:EE:FF")


async def test_the_esphome_key_itself_does_not_open_the_channel(show: FakeShow) -> None:
    """The channel's key is derived from the pairing key: a handshake made with the pairing key itself is refused."""
    assert channel_key(PAIRING_KEY) != PAIRING_KEY
    with pytest.raises(AuthError):
        await Channel.connect("127.0.0.1", show.port, PAIRING_KEY[::-1])


async def test_a_show_with_another_address_is_not_the_one_asked_for(show: FakeShow) -> None:
    with pytest.raises(ChannelError, match="not the Show"):
        await Channel.connect("127.0.0.1", show.port, PAIRING_KEY, expected_mac="11:22:33:44:55:66")


async def test_nothing_listening_is_cannot_connect() -> None:
    with pytest.raises(CannotConnect):
        await Channel.connect("127.0.0.1", 1, PAIRING_KEY, timeout=1)


async def test_a_plain_protocol_peer_is_not_taken_for_a_show() -> None:
    async def plain(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(100)
        writer.write(b"\x00\x00\x05hello")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(plain, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ChannelError, match="plain"):
            await Channel.connect("127.0.0.1", port, PAIRING_KEY, timeout=2)
    finally:
        server.close()
        await server.wait_closed()


async def test_a_message_longer_than_the_protocol_allows_is_not_sent(show: FakeShow) -> None:
    channel = await Channel.connect("127.0.0.1", show.port, PAIRING_KEY)
    with pytest.raises(ProtocolError):
        await channel.send(message("states", "snapshot", filler="x" * MAX_MESSAGE))
    await channel.close()


def test_the_codec_refuses_what_is_not_a_message() -> None:
    for bad in (b"not json", b"[1]", b'{"m":"core"}', b'{"m":1,"t":"x"}'):
        with pytest.raises(ProtocolError):
            decode(bad)
    assert decode(encode(message("core", "ping", n=1))) == message("core", "ping", n=1)
