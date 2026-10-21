"""Module ``alarms``: the calendar as the source of truth, compare-and-swap, hand edits, the relay. A real Local Calendar and fake hubs; the last
tests go through the real hub and the fake Show."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import Event, HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumashow import alarms as alarms_module
from custom_components.lumashow.alarms import CALENDAR_ENTITY, EVENT_ALARM, AlarmsModule
from custom_components.lumashow.const import DOMAIN, OPT_SHARE_ALARMS

from .conftest import MAC, PAIRING_KEY, entry_data, setup
from .fake_show import FakeShow

GYM: dict[str, Any] = {"label": "Gym", "time": "06:30", "days": ["mon", "tue", "wed", "thu", "fri"], "on": True, "volume": 70, "ring": "every"}
SOON = (dt.date.today() + dt.timedelta(days=3)).isoformat()  # inside the window the calendar is read in
ONCE: dict[str, Any] = {"label": "Flight", "time": "05:15", "days": [], "date": SOON, "on": True, "volume": 100, "ring": "every"}
AT = 1_792_500_000


class FakeHub:
    """What the module needs of a hub: its name, its options, and a way to send."""

    def __init__(self, name: str, *, sharing: bool = True) -> None:
        self.name = name.title()
        self.device = {"name": name}
        self.device_name = name
        self.entry = SimpleNamespace(entry_id=f"entry-{name}")
        self.options = {OPT_SHARE_ALARMS: sharing}
        self.agreed = {"alarms": 1}
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def async_send_module(self, module: str, kind: str, fields: Any) -> bool:
        assert module == "alarms"
        self.sent.append((kind, dict(fields)))
        return True

    def kinds(self, kind: str) -> list[dict[str, Any]]:
        return [fields for k, fields in self.sent if k == kind]

    def last(self, kind: str) -> dict[str, Any]:
        return self.kinds(kind)[-1]


async def until(hass: HomeAssistant, condition: Callable[[], bool], what: str = "the condition", seconds: float = 6.0) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.02)
            await hass.async_block_till_done()
    assert condition(), what


async def forget_calendar_file(hass: HomeAssistant) -> None:
    """The calendar's file is in the folder Home Assistant's test config keeps: it is not left there."""
    await hass.async_add_executor_job(lambda: Path(hass.config.path(".storage/local_calendar.lumashow_alarms.ics")).unlink(missing_ok=True))


def local_calendar_entry(hass: HomeAssistant) -> MockConfigEntry:
    """The person's one step in Home Assistant: a Local Calendar called LumaShow alarms (its file is kept in a folder of this test)."""
    Path(hass.config.path(".storage")).mkdir(parents=True, exist_ok=True)
    config = MockConfigEntry(domain="local_calendar", data={"calendar_name": "LumaShow alarms", "storage_key": "lumashow_alarms"})
    config.add_to_hass(hass)
    return config


@pytest.fixture
async def calendar(hass: HomeAssistant, enable_custom_integrations: None) -> AsyncGenerator[Any]:
    config = local_calendar_entry(hass)
    assert await hass.config_entries.async_setup(config.entry_id)
    await hass.async_block_till_done()
    entity = hass.data[DATA_COMPONENT].get_entity(CALENDAR_ENTITY)
    assert entity is not None
    yield entity
    await forget_calendar_file(hass)


@pytest.fixture
async def module(hass: HomeAssistant) -> AsyncGenerator[AlarmsModule]:
    module = AlarmsModule(hass)
    yield module
    await module._async_stop()


def put(op: int, uid: str, expect: int, alarm: dict[str, Any], at: int = AT) -> dict[str, Any]:
    return {"op": op, "uid": uid, "expect": expect, "alarm": alarm, "at": at}


async def events(hass: HomeAssistant, calendar: Any) -> list[dict[str, Any]]:
    start = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    return [e.as_dict() for e in await calendar.async_get_events(hass, start, start + dt.timedelta(days=20))]


async def up(module: AlarmsModule, *hubs: FakeHub) -> None:
    for hub in hubs:
        await module.async_session(hub, True)  # type: ignore[arg-type]


async def say(module: AlarmsModule, hub: FakeHub, kind: str, body: dict[str, Any]) -> None:
    await module.async_message(hub, kind, body)  # type: ignore[arg-type]


# -- the snapshot -------------------------------------------------------------------------------------------------------------------


async def test_a_show_that_shares_alarms_is_told_what_the_calendar_holds_and_the_zone(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    hub = FakeHub("lumashow-black")
    await up(module, hub)
    snapshot = hub.last("snapshot")
    assert snapshot["ready"] is True and snapshot["alarms"] == []
    assert snapshot["tz"] == hass.config.time_zone
    assert snapshot["rev"] >= 1


async def test_nothing_is_sent_to_a_show_that_does_not_share_alarms_and_its_messages_are_not_read(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    quiet = FakeHub("lumashow-quiet", sharing=False)
    assert module.offers(quiet) is False  # type: ignore[arg-type]
    await up(module, quiet)
    await say(module, quiet, "put", put(1, "ls-1", 0, GYM))
    await say(module, quiet, "resync", {})
    assert quiet.sent == []
    assert await events(hass, calendar) == []
    assert module._unsubscribe is None and not module._timers, "nothing listens and nothing is timed while no Show shares"


async def test_a_calendar_that_is_not_there_says_so_and_nothing_is_believed_deleted_and_it_is_found_when_it_comes(
    hass: HomeAssistant, enable_custom_integrations: None, module: AlarmsModule, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(alarms_module, "RETRY_EVERY", dt.timedelta(seconds=0.05))
    hub = FakeHub("lumashow-black")
    await up(module, hub)
    first = hub.last("snapshot")
    assert first["ready"] is False and first["alarms"] == []
    # A write is refused, not lost.
    await say(module, hub, "put", put(1, "ls-1", 0, GYM))
    result = hub.last("result")
    assert result["ok"] is False and result["code"] == "unavailable"
    # The person adds the calendar: it is found without anybody being asked.
    config = local_calendar_entry(hass)
    assert await hass.config_entries.async_setup(config.entry_id)
    await until(hass, lambda: hub.last("snapshot")["ready"] is True, "the calendar to be found")
    assert hub.last("snapshot")["rev"] > first["rev"], "rev rises with every change of anything the snapshot says"
    await forget_calendar_file(hass)


# -- a Show writes ------------------------------------------------------------------------------------------------------------------


async def test_a_put_makes_the_event_the_snapshot_and_the_result_and_is_stamped_with_the_sessions_name_not_the_shows(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black, white = FakeHub("lumashow-black"), FakeHub("lumashow-white")
    await up(module, black, white)
    before = white.last("snapshot")["rev"]
    await say(module, black, "put", {**put(7, "ls-aaa111", 0, {**GYM, "sound": "dawn", "ring": "one"}), "by": "someone-else"})
    result = black.last("result")
    assert result == {"op": 7, "ok": True, "uid": "ls-aaa111", "rev": 1}
    listed = await events(hass, calendar)
    assert len(listed) >= 5, "a weekday series is listed once a day"
    first = listed[0]
    assert first["uid"] == "ls-aaa111" and first["summary"] == "Gym" and first["rrule"] == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    assert first["start"][11:16] == "06:30"
    assert first["description"] == "volume: 70\nsound: dawn\nring: one\nedited: 1792500000 lumashow-black", "by is the session's name"
    for hub in (black, white):
        snapshot = hub.last("snapshot")
        assert snapshot["rev"] > before
        (alarm,) = snapshot["alarms"]
        assert (alarm["uid"], alarm["rev"], alarm["by"], alarm["at"]) == ("ls-aaa111", 1, "lumashow-black", AT)
        assert (alarm["label"], alarm["time"], alarm["volume"], alarm["sound"], alarm["ring"]) == ("Gym", "06:30", 70, "dawn", "one")


async def test_a_put_against_the_current_rev_is_taken_and_one_against_an_old_rev_is_answered_with_the_alarm_as_it_is(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black, white = FakeHub("lumashow-black"), FakeHub("lumashow-white")
    await up(module, black, white)
    await say(module, black, "put", put(1, "ls-1", 0, GYM))
    await say(module, white, "put", put(2, "ls-1", 1, {**GYM, "volume": 20}, at=AT + 5))
    assert white.last("result") == {"op": 2, "ok": True, "uid": "ls-1", "rev": 2}
    # Black, which has not heard yet, writes against rev 1: it lost the race.
    await say(module, black, "put", put(3, "ls-1", 1, {**GYM, "label": "Black's"}, at=AT + 9))
    lost = black.last("result")
    assert lost["ok"] is False and lost["code"] == "conflict" and lost["uid"] == "ls-1"
    assert lost["current"]["rev"] == 2 and lost["current"]["volume"] == 20 and lost["current"]["by"] == "lumashow-white"
    assert (await events(hass, calendar))[0]["summary"] == "Gym", "the lost write changed nothing"
    # A write to an alarm that is not there, as if it were (a deletion elsewhere): the Show is told it is gone.
    await say(module, black, "put", put(4, "ls-gone", 3, GYM))
    gone = black.last("result")
    assert gone["code"] == "conflict" and gone["current"] is None


async def test_what_a_person_did_in_the_calendar_a_moment_ago_is_counted_before_a_write_is_judged(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black = FakeHub("lumashow-black")
    await up(module, black)
    await say(module, black, "put", put(1, "ls-1", 0, GYM))
    # The person changes the title in the calendar panel; the Show, which has not been told, writes against rev 1 at once.
    await calendar.async_update_event(
        "ls-1",
        {
            "summary": "Gym (changed by hand)",
            "description": "volume: 70\nedited: 1792500000 lumashow-black",
            "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
            "dtstart": dt.datetime(2026, 10, 21, 6, 30),
            "dtend": dt.datetime(2026, 10, 21, 6, 31),
        },
    )
    await say(module, black, "put", put(2, "ls-1", 1, {**GYM, "volume": 10}, at=AT + 1))
    lost = black.last("result")
    assert lost["code"] == "conflict", "the hand edit is revision 2: the Show's write was based on revision 1"
    assert lost["current"]["rev"] == 2 and lost["current"]["label"] == "Gym (changed by hand)"
    assert lost["current"]["by"] == "lumashow-black" and lost["current"]["at"] == AT, "a person's edit leaves the Show's stamp on it"


async def test_an_edit_in_the_calendar_panel_is_a_new_rev_pushed_to_every_show_and_a_switch_is_typed_as_a_prefix(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black, white = FakeHub("lumashow-black"), FakeHub("lumashow-white")
    await up(module, black, white)
    await say(module, black, "put", put(1, "ls-1", 0, GYM))
    await until(hass, lambda: white.last("snapshot")["alarms"][0]["rev"] == 1, "the first snapshot")
    base = await events(hass, calendar)
    await calendar.async_update_event(
        "ls-1",
        {
            "summary": "(off) Gym",
            "description": base[0]["description"],
            "rrule": base[0]["rrule"],
            "dtstart": dt.datetime(2026, 10, 21, 6, 45),
            "dtend": dt.datetime(2026, 10, 21, 6, 46),
        },
    )
    await until(hass, lambda: white.last("snapshot")["alarms"][0]["rev"] == 2, "the hand edit to reach the other Show", seconds=8)
    alarm = white.last("snapshot")["alarms"][0]
    assert (alarm["on"], alarm["time"], alarm["label"]) == (False, "06:45", "Gym")
    assert (alarm["by"], alarm["at"]) == ("lumashow-black", AT), "nothing says a Show made this edit"
    # Nothing was written back by the module for it.
    assert len([e for e in await events(hass, calendar) if e["uid"] == "ls-1"]) >= 5


async def test_an_event_made_by_hand_is_an_alarm_that_no_show_has_edited_and_an_event_a_show_cannot_keep_is_left_alone(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black = FakeHub("lumashow-black")
    await up(module, black)
    await calendar.async_create_event(summary="Dentist", dtstart=dt.datetime(2026, 10, 22, 9, 0), dtend=dt.datetime(2026, 10, 22, 9, 30))
    await calendar.async_create_event(
        summary="Rent", dtstart=dt.datetime(2026, 10, 3, 9, 0), dtend=dt.datetime(2026, 10, 3, 9, 30), rrule="FREQ=MONTHLY;BYMONTHDAY=3"
    )
    await until(hass, lambda: any(a["label"] == "Dentist" for a in black.last("snapshot")["alarms"]), "the hand-made alarm")
    alarms = black.last("snapshot")["alarms"]
    assert [a["label"] for a in alarms] == ["Dentist"], "the monthly event is not an alarm"
    dentist = alarms[0]
    assert (dentist["by"], dentist["at"], dentist["rev"], dentist["volume"], dentist["ring"], dentist["date"]) == (
        "",
        0,
        1,
        100,
        "every",
        "2026-10-22",
    )
    assert dentist["days"] == [] and dentist["time"] == "09:00"


async def test_a_once_only_alarm_carries_its_date_and_turning_a_series_into_one_takes_the_rule_away(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black = FakeHub("lumashow-black")
    await up(module, black)
    await say(module, black, "put", put(1, "ls-1", 0, ONCE))
    (event,) = [e for e in await events(hass, calendar) if e["uid"] == "ls-1"]
    assert event["start"].startswith(f"{SOON}T05:15") and "rrule" not in event
    # The same alarm made a series, then a once-only alarm again: the rule goes when the days do.
    await say(module, black, "put", put(2, "ls-1", 1, {**GYM, "label": "Flight"}, at=AT + 1))
    assert black.last("result")["ok"] is True
    assert next(e for e in await events(hass, calendar) if e["uid"] == "ls-1")["rrule"] == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    await say(module, black, "put", put(3, "ls-1", 2, ONCE, at=AT + 2))
    assert black.last("result")["ok"] is True
    after = [e for e in await events(hass, calendar) if e["uid"] == "ls-1"]
    assert len(after) == 1 and "rrule" not in after[0] and after[0]["start"].startswith(f"{SOON}T05:15")


async def test_a_delete_is_taken_against_the_current_rev_and_the_alarm_leaves_every_show(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black, white = FakeHub("lumashow-black"), FakeHub("lumashow-white")
    await up(module, black, white)
    await say(module, black, "put", put(1, "ls-1", 0, GYM))
    await say(module, white, "delete", {"op": 2, "uid": "ls-1", "expect": 0})
    assert white.last("result")["code"] == "conflict", "white's copy is older than the calendar's: not deleted"
    await say(module, white, "delete", {"op": 3, "uid": "ls-1", "expect": 1})
    assert white.last("result") == {"op": 3, "ok": True, "uid": "ls-1", "rev": 1}
    assert [e for e in await events(hass, calendar) if e["uid"] == "ls-1"] == []
    assert black.last("snapshot")["alarms"] == []


async def test_what_is_malformed_or_too_many_is_refused_with_its_code(hass: HomeAssistant, calendar: Any, module: AlarmsModule) -> None:
    black = FakeHub("lumashow-black")
    await up(module, black)
    await say(module, black, "put", put(1, "has space", 0, GYM))
    assert black.last("result")["code"] == "bad_alarm"
    await say(module, black, "put", put(2, "ls-1", 0, {**GYM, "volume": 0}))
    assert (
        black.last("result") == {"op": 2, "ok": False, "code": "bad_alarm", "message": "`volume` is not from 1 to 100"}
        or black.last("result")["code"] == "bad_alarm"
    )
    await say(module, black, "put", {"op": 3, "uid": "ls-1", "expect": "zero", "alarm": GYM, "at": AT})
    assert black.last("result")["code"] == "bad_alarm"
    assert await events(hass, calendar) == []
    for n in range(alarms_module.MAX_ALARMS):
        await say(module, black, "put", put(10 + n, f"ls-{n}", 0, {**GYM, "label": f"Alarm {n}"}))
        assert black.last("result")["ok"] is True, n
    await say(module, black, "put", put(99, "ls-one-too-many", 0, GYM))
    assert black.last("result")["code"] == "too_many"
    # An alarm that is there can still be changed at the limit.
    await say(module, black, "put", put(100, "ls-0", 1, {**GYM, "label": "Still editable"}))
    assert black.last("result")["ok"] is True


# -- what the Shows tell each other -------------------------------------------------------------------------------------------------


async def test_a_beacon_and_an_answer_go_to_the_other_shows_with_the_sessions_name_and_an_answer_is_an_event_for_automations(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black, white, quiet = FakeHub("lumashow-black"), FakeHub("lumashow-white"), FakeHub("lumashow-quiet", sharing=False)
    await up(module, black, white, quiet)
    fired: list[Event] = []
    hass.bus.async_listen(EVENT_ALARM, fired.append)
    await say(module, black, "beacon", {"age": 12, "prio": 1, "from": "lumashow-forged"})
    assert white.last("beacon") == {"from": "lumashow-black", "age": 12, "prio": 1}
    assert black.kinds("beacon") == [] and quiet.sent == []
    await say(module, black, "beacon", {"age": None})
    assert white.last("beacon") == {"from": "lumashow-black", "age": None, "prio": 0}
    await say(module, black, "answered", {"occ": "ls-1@2026-10-21T06:30", "how": "stopped"})
    assert white.last("answered") == {"from": "lumashow-black", "occ": "ls-1@2026-10-21T06:30", "how": "stopped"}
    await hass.async_block_till_done()
    assert [e.data for e in fired] == [{"kind": "answered", "from": "lumashow-black", "occ": "ls-1@2026-10-21T06:30", "how": "stopped"}]
    # What is malformed is not passed on.
    before = len(white.sent)
    for bad in ({"occ": "has space", "how": "stopped"}, {"occ": "ls-1@x", "how": "shrugged"}, {"how": "stopped"}):
        await say(module, black, "answered", bad)
    for bad in ({"age": -1}, {"age": "soon"}, {"prio": 300}):
        await say(module, black, "beacon", bad)
    assert len(white.sent) == before and len(fired) == 1


async def test_a_resync_is_answered_with_the_snapshot_and_a_session_that_ends_leaves_nothing_listening(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black = FakeHub("lumashow-black")
    await up(module, black)
    count = len(black.kinds("snapshot"))
    await say(module, black, "resync", {})
    assert len(black.kinds("snapshot")) == count + 1
    assert module._unsubscribe is not None
    await module.async_session(black, False)  # type: ignore[arg-type]
    assert module._unsubscribe is None and not module._timers
    await say(module, black, "put", put(1, "ls-1", 0, GYM))
    assert black.kinds("result") == [], "a Show whose session is over is not heard"


async def test_the_revs_are_kept_so_that_a_restart_does_not_make_an_old_write_look_current(
    hass: HomeAssistant, calendar: Any, module: AlarmsModule
) -> None:
    black = FakeHub("lumashow-black")
    await up(module, black)
    await say(module, black, "put", put(1, "ls-1", 0, GYM))
    await say(module, black, "put", put(2, "ls-1", 1, {**GYM, "volume": 30}, at=AT + 1))
    rev = black.last("snapshot")["rev"]
    await module._store.async_save(module._meta)
    again = AlarmsModule(hass)
    other = FakeHub("lumashow-white")
    await up(again, other)
    snapshot = other.last("snapshot")
    assert snapshot["alarms"][0]["rev"] == 2
    assert snapshot["rev"] >= rev, "the set's rev never falls"
    await again._async_stop()


# -- through the real hub -----------------------------------------------------------------------------------------------------------


async def test_alarms_are_offered_only_to_a_show_that_shares_them_and_a_show_can_write_and_read_through_the_channel(
    hass: HomeAssistant, calendar: Any, socket_enabled: None
) -> None:
    fake = FakeShow(PAIRING_KEY, modules={"states": 1, "actions": 1, "cameras": 1, "notify": 1, "alarms": 1})
    await fake.start()
    config = MockConfigEntry(domain=DOMAIN, title="Living Room Show", unique_id=MAC, data=entry_data(fake.port), options={OPT_SHARE_ALARMS: True})
    config.add_to_hass(hass)
    try:
        await setup(hass, config)
        session = await fake.wait_session()
        hello = await session.wait_kind("core", "hello")
        assert hello["modules"]["alarms"] == 1
        snapshot = await session.wait_kind("alarms", "snapshot")
        assert snapshot["ready"] is True and snapshot["alarms"] == []
        await session.send({"m": "alarms", "t": "put", **put(5, "ls-e2e", 0, GYM)})
        result = await session.wait(lambda m: m["m"] == "alarms" and m["t"] == "result")
        assert result["ok"] is True and result["rev"] == 1 and result["op"] == 5
        pushed = await session.wait(lambda m: m["m"] == "alarms" and m["t"] == "snapshot" and bool(m["alarms"]))
        assert pushed["alarms"][0]["by"] == "lumashow-abcdef" or pushed["alarms"][0]["by"], "stamped with a name"
        await session.send({"m": "alarms", "t": "answered", "occ": "ls-e2e@2026-10-21T06:30", "how": "snoozed"})
    finally:
        await fake.stop()


async def test_a_show_that_does_not_share_alarms_is_not_offered_the_module(hass: HomeAssistant, calendar: Any, socket_enabled: None) -> None:
    fake = FakeShow(PAIRING_KEY, modules={"states": 1, "actions": 1, "cameras": 1, "notify": 1, "alarms": 1})
    await fake.start()
    config = MockConfigEntry(domain=DOMAIN, title="Living Room Show", unique_id=MAC, data=entry_data(fake.port), options={})
    config.add_to_hass(hass)
    try:
        await setup(hass, config)
        session = await fake.wait_session()
        hello = await session.wait_kind("core", "hello")
        assert "alarms" not in hello["modules"]
        await session.send({"m": "alarms", "t": "resync"})
        await asyncio.sleep(0.2)
        assert session.kinds("alarms", "snapshot") == []
    finally:
        await fake.stop()
