"""The alarms' channel JSON and calendar event format, against the file the Show's own tests decode (``tests/vectors/alarms.json``)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.lumashow.alarms_codec import (
    Fields,
    Record,
    Stamp,
    days_of,
    fields_from_json,
    fields_to_json,
    read_events,
    record_to_json,
    rrule_of,
    write_event,
)

VECTORS: dict[str, Any] = json.loads((Path(__file__).parent / "vectors" / "alarms.json").read_text())


def stamp_of(value: Any) -> Stamp | None:
    return None if value is None else Stamp(value["by"], value["at"])


@pytest.mark.parametrize("name", [n for n in VECTORS if n.startswith("event.write.")])
def test_what_is_written_to_the_calendar_is_what_the_show_writes(name: str) -> None:
    case = VECTORS[name]
    written = write_event(
        fields_from_json(case["fields"]),
        stamp_of(case["stamp"]) or Stamp(),
        case["extra"],
        dt.date.fromisoformat(case["start_date"]),
        case["offset_secs"],
    )
    assert written.summary == case["written"]["summary"]
    assert written.description == case["written"]["description"]
    assert written.dtstart == case["written"]["dtstart"]
    assert written.dtend == case["written"]["dtend"]
    assert written.rrule == case["written"]["rrule"]


@pytest.mark.parametrize("name", [n for n in VECTORS if n.startswith("event.read.")])
def test_what_is_read_from_the_calendar_is_what_the_show_reads(name: str) -> None:
    case = VECTORS[name]
    alarms, skipped = read_events([case["event"]])
    result = case["result"]
    if "skipped" in result:
        assert not alarms and [why for _, why in skipped] == [result["skipped"]]
        return
    assert not skipped
    assert len(alarms) == 1
    expected = result["alarm"]
    assert fields_to_json(alarms[0].fields) == expected["fields"]
    assert alarms[0].stamp == stamp_of(expected["stamp"])
    assert alarms[0].extra == expected["extra"]


def test_the_alarms_of_the_vectors_survive_the_channel_both_ways() -> None:
    for name in ("to_show.snapshot", "to_show.result_conflict"):
        body = VECTORS[name]["body"]
        records = body.get("alarms") or [body["current"]]
        for item in records:
            fields = fields_from_json(item)
            record = Record(item["uid"], item["rev"], fields, Stamp(item["by"], item["at"]))
            assert record_to_json(record) == item
    put = VECTORS["from_show.put_once"]["body"]
    assert fields_to_json(fields_from_json(put["alarm"])) == put["alarm"]


@pytest.mark.parametrize(
    "change",
    [
        {"label": "x" * 61},
        {"label": "a\nb"},
        {"time": "7"},
        {"time": "24:00"},
        {"days": ["mon", "someday"]},
        {"volume": 0},
        {"volume": 101},
        {"volume": True},
        {"ring": "loud"},
        {"sound": "not a word"},
        {"on": "yes"},
        {"date": "2026-10-21"},  # a series with a date
        {"days": [], "date": None},  # a once-only alarm with none
        {"days": [], "date": "2026-13-01"},
    ],
)
def test_an_alarm_that_is_malformed_is_refused(change: dict[str, Any]) -> None:
    good = dict(VECTORS["from_show.put"]["body"]["alarm"])
    assert fields_from_json(good)
    with pytest.raises(ValueError):
        fields_from_json({**good, **change})


def test_the_days_of_a_rule_and_the_rule_of_the_days_are_each_others_and_what_a_show_cannot_keep_is_said() -> None:
    assert rrule_of(range(7)) == "FREQ=DAILY"
    assert rrule_of([]) is None
    assert rrule_of({0, 4}) == "FREQ=WEEKLY;BYDAY=MO,FR"
    assert days_of("FREQ=WEEKLY;BYDAY=MO,FR") == frozenset({0, 4})
    assert days_of("RRULE:FREQ=DAILY") == frozenset(range(7))
    for rule in ("FREQ=MONTHLY;BYMONTHDAY=3", "FREQ=DAILY;INTERVAL=2", "FREQ=WEEKLY", "FREQ=DAILY;COUNT=3", "FREQ=WEEKLY;BYDAY=1MO"):
        with pytest.raises(ValueError):
            days_of(rule)


def test_a_series_listed_once_a_day_is_one_alarm_and_the_earliest_is_its_first() -> None:
    event = VECTORS["event.read.series_with_settings"]["event"]
    later = {**event, "start": "2026-10-26T07:05:00-04:00"}
    alarms, _ = read_events([later, event])
    assert len(alarms) == 1
    assert Fields.key(alarms[0].fields)[:3] == ("Wake up", 7, 5)
