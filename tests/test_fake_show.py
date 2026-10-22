"""The fake Show's own teardown: a connection made while it stops is refused and leaves no task behind.

A flake of the first public runs: the integration reconnected as a test ended. A connection that was being accepted when the server closed was made
a step later, in the middle of `stop()`, after `stop()` had looked at its tasks and while it waited for the connections to close: nobody cancelled
its task, `wait_closed` timed out, and the test plugin failed the test for the lingering task.
"""

from __future__ import annotations

import asyncio

import pytest

from .fake_show import FakeShow

PAIRING_KEY = bytes(range(32))


class Writer:
    """What the stream protocol hands the callback of a connection that was in flight."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def test_a_connection_made_in_the_middle_of_stop_is_refused_and_no_task_is_left(socket_enabled: None) -> None:
    fake = FakeShow(PAIRING_KEY)
    await fake.start()
    await fake.stop()
    # The connection of the flake: made after `stop()` began (the server's callback is called by the stream protocol for a connection in flight).
    writer = Writer()

    async def serving() -> None:
        fake._accept(asyncio.StreamReader(), writer)  # type: ignore[arg-type]

    await serving()
    assert writer.closed, "a connection made while the Show stops is refused"
    assert not fake._tasks, "and nothing is served that stop() has not seen"
    assert not [task for task in asyncio.all_tasks() if "FakeShow._serve" in repr(task)]


@pytest.mark.parametrize("steps", range(8))
async def test_a_connection_made_before_stop_is_cancelled_by_it(socket_enabled: None, steps: int) -> None:
    fake = FakeShow(PAIRING_KEY)
    port = await fake.start()
    # A client that has connected and said nothing: the Show's end waits for its hello. `stop()` comes some steps of the loop after the connect.
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    for _ in range(steps):
        await asyncio.sleep(0)
    await fake.stop()
    assert not [task for task in asyncio.all_tasks() if "FakeShow._serve" in repr(task) and not task.done()]
    writer.close()
    await asyncio.sleep(0)
