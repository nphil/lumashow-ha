"""The alarms of module ``alarms``: their JSON on the channel and their form as an event of a Home Assistant calendar.

This is the Python twin of the Show's ``alarm-sync`` crate (``wire.rs`` and ``event.rs``); ``tests/vectors/alarms.json`` is the same file the
Rust tests decode, so the two cannot drift. Nothing here touches Home Assistant: it is data in, data out.

An alarm is one event of a Local Calendar (docs/protocol.md, "alarms"): the *summary* is its label (``(off) `` in front when it is off), the
start's time of day is its time, the recurrence rule is its days (none: a once-only alarm, whose date is the start's), and a few lines at the top
of the *description* carry the rest (``volume``, ``sound``, ``ring``, ``edited``); whatever else a person wrote there is kept.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Final

DAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAYS: Final = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
MAX_LABEL: Final = 60
MAX_ALARMS: Final = 32
OFF_PREFIX: Final = "(off) "
NO_LABEL: Final = "Alarm"
_PLAIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:@")


def plain_word(text: Any, longest: int) -> bool:
    """A text a device, an alarm or an occurrence may be named by: printable ASCII with no spaces, bounded."""
    return isinstance(text, str) and 0 < len(text) <= longest and all(c in _PLAIN for c in text)


@dataclass(frozen=True)
class Fields:
    """What two copies of an alarm are compared by."""

    label: str
    hour: int
    minute: int
    days: frozenset[int]  # 0 is Monday; none: the alarm rings once
    enabled: bool
    volume: int  # 1 to 100, percent of the speaker's safe maximum
    sound: str | None  # a ring of the sound library; None: the Sound page's own
    ring: str  # "every": every device rings; "one": the nearest, the others a minute later if nobody answered
    date: str | None  # a once-only alarm's date, YYYY-MM-DD

    def key(self) -> tuple[Any, ...]:
        """What a person can change by hand in the calendar panel (the signature a hand edit is noticed by)."""
        return (self.label, self.hour, self.minute, tuple(sorted(self.days)), self.enabled, self.volume, self.sound, self.ring, self.date)


@dataclass(frozen=True)
class Stamp:
    """Who last edited an alarm on a Show, and when by that Show's clock (seconds since the epoch). A person's edit leaves it alone."""

    by: str = ""
    at: int = 0


@dataclass(frozen=True)
class Record:
    """An alarm as the integration holds it."""

    uid: str
    rev: int
    fields: Fields
    stamp: Stamp


# -- the channel's JSON ---------------------------------------------------------------------------------------------------------------


def fields_to_json(fields: Fields) -> dict[str, Any]:
    out: dict[str, Any] = {
        "label": fields.label,
        "time": f"{fields.hour:02d}:{fields.minute:02d}",
        "days": [DAYS[d] for d in sorted(fields.days)],
    }
    if fields.date is not None:
        out["date"] = fields.date
    out["on"] = fields.enabled
    out["volume"] = fields.volume
    if fields.sound is not None:
        out["sound"] = fields.sound
    out["ring"] = fields.ring
    return out


def record_to_json(record: Record) -> dict[str, Any]:
    return {**fields_to_json(record.fields), "uid": record.uid, "rev": record.rev, "by": record.stamp.by, "at": record.stamp.at}


def _parse_date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError as err:
        raise ValueError("`date` is not YYYY-MM-DD") from err


def fields_from_json(value: Any) -> Fields:
    """An alarm's fields as a message gives them, checked as if they were hostile. ``ValueError`` says what is wrong."""
    if not isinstance(value, Mapping):
        raise ValueError("an alarm is an object")
    label = value.get("label")
    if not isinstance(label, str) or len(label) > MAX_LABEL or any(ord(c) < 32 or ord(c) == 127 for c in label):
        raise ValueError("`label` is missing, too long or has control characters")
    time = value.get("time")
    if not isinstance(time, str) or len(time) != 5 or time[2] != ":" or not time[:2].isdigit() or not time[3:].isdigit():
        raise ValueError("`time` is not HH:MM")
    hour, minute = int(time[:2]), int(time[3:])
    if hour > 23 or minute > 59:
        raise ValueError("`time` is not a time of day")
    raw_days = value.get("days")
    if not isinstance(raw_days, list):
        raise ValueError("`days` is missing")
    days: set[int] = set()
    for name in raw_days:
        if name not in DAYS:
            raise ValueError("a day is not a weekday")
        days.add(DAYS.index(name))
    date = value.get("date")
    if date is not None:
        if not isinstance(date, str):
            raise ValueError("`date` is not text")
        _parse_date(date)
    if (not days) != (date is not None):
        raise ValueError("an alarm that repeats has no `date`, and one that does not repeat has one")
    volume = value.get("volume")
    if isinstance(volume, bool) or not isinstance(volume, int) or not 1 <= volume <= 100:
        raise ValueError("`volume` is not from 1 to 100")
    sound = value.get("sound")
    if sound is not None and not plain_word(sound, 64):
        raise ValueError("`sound` is not a plain word")
    ring = value.get("ring")
    if ring not in ("one", "every"):
        raise ValueError("`ring` is not `one` or `every`")
    enabled = value.get("on")
    if not isinstance(enabled, bool):
        raise ValueError("`on` is missing")
    return Fields(label, hour, minute, frozenset(days), enabled, volume, sound, ring, date)


# -- an alarm as an event of the calendar ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Written:
    """What is written to the calendar for an alarm (the times are ISO text with the zone's offset, as the Show's twin writes them)."""

    summary: str
    description: str
    dtstart: str
    dtend: str
    rrule: str | None


@dataclass(frozen=True)
class Parsed:
    """An alarm as the calendar holds it, before the integration has numbered it."""

    uid: str
    fields: Fields
    stamp: Stamp | None  # None: no Show has edited it (it was made by hand)
    extra: str  # what the person wrote in the description besides the settings' lines


def rrule_of(days: Iterable[int]) -> str | None:
    chosen = sorted(set(days))
    if not chosen:
        return None
    if len(chosen) == 7:
        return "FREQ=DAILY"
    return "FREQ=WEEKLY;BYDAY=" + ",".join(WEEKDAYS[d] for d in chosen)


def days_of(rule: str) -> frozenset[int]:
    """The days of a recurrence rule; ``ValueError`` says why a Show cannot keep that alarm."""
    rule = rule.removeprefix("RRULE:")
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    if any(k not in ("FREQ", "BYDAY") for k in parts):
        raise ValueError(f"the rule {rule} has more in it than days of the week")
    freq, byday = parts.get("FREQ"), parts.get("BYDAY")
    if freq == "DAILY" and byday is None:
        return frozenset(range(7))
    if freq == "WEEKLY" and byday is not None:
        chosen: set[int] = set()
        for word in byday.split(","):
            if word not in WEEKDAYS:
                raise ValueError(f"the day {word} is not a plain weekday")
            chosen.add(WEEKDAYS.index(word))
        return frozenset(chosen)
    raise ValueError(f"the rule {rule} is not daily or on named weekdays")


def _description_of(fields: Fields, stamp: Stamp, extra: str) -> str:
    lines = []
    if fields.volume != 100:
        lines.append(f"volume: {fields.volume}")
    if fields.sound is not None:
        lines.append(f"sound: {fields.sound}")
    if fields.ring == "one":
        lines.append("ring: one")
    if stamp.by:
        lines.append(f"edited: {stamp.at} {stamp.by}")
    return ("\n".join(lines) + "\n" + extra).rstrip()


def _read_description(text: str) -> tuple[int, str | None, str, Stamp | None, str]:
    volume, sound, ring, stamp = 100, None, "every", None
    extra: list[str] = []
    for line in text.splitlines():
        if ":" not in line:
            extra.append(line)
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        match key.strip():
            case "volume":
                digits = value.removesuffix("%")
                if digits.isdigit() and 1 <= int(digits) <= 100:
                    volume = int(digits)
            case "sound":
                if plain_word(value, 64):
                    sound = value
            case "ring":
                ring = "one" if value.lower() == "one" else "every"
            case "edited":
                at, _, by = value.partition(" ")
                if at.lstrip("-").isdigit() and plain_word(by.strip(), 40):
                    stamp = Stamp(by.strip(), int(at))
            case _:
                extra.append(line)
    return volume, sound, ring, stamp, "\n".join(extra).strip()


def _stamp(day: dt.date, minutes: int, offset_secs: int) -> str:
    day = day + dt.timedelta(days=minutes // 1440)
    minutes %= 1440
    sign, off = ("-", -offset_secs) if offset_secs < 0 else ("+", offset_secs)
    return f"{day.isoformat()}T{minutes // 60:02d}:{minutes % 60:02d}:00{sign}{off // 3600:02d}:{off % 3600 // 60:02d}"


def write_event(fields: Fields, stamp: Stamp, extra: str, start_date: dt.date, offset_secs: int) -> Written:
    """What to write for an alarm: the first occurrence is on ``start_date`` (a once-only alarm's own date is used instead), lasts a minute."""
    day = start_date
    if not fields.days and fields.date is not None:
        day = _parse_date(fields.date)
    minutes = fields.hour * 60 + fields.minute
    label = fields.label or NO_LABEL
    return Written(
        summary=label if fields.enabled else f"{OFF_PREFIX}{label}",
        description=_description_of(fields, stamp, extra),
        dtstart=_stamp(day, minutes, offset_secs),
        dtend=_stamp(day, minutes + 1, offset_secs),
        rrule=rrule_of(fields.days),
    )


def _stamp_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        found = value.get("dateTime") or value.get("date")
        return found if isinstance(found, str) else None
    return None


def read_event(uid: str, event: Mapping[str, Any]) -> Parsed:
    """One listed event as an alarm; ``ValueError`` says why it is not one a Show can have."""
    start = _stamp_text(event.get("start"))
    if start is None:
        raise ValueError("it has no start")
    if "T" not in start:
        raise ValueError("it is an all-day event")
    date, time = start.split("T", 1)
    if len(time) < 5 or time[2] != ":" or not time[:2].isdigit() or not time[3:5].isdigit() or int(time[:2]) > 23 or int(time[3:5]) > 59:
        raise ValueError("its start is not a time")
    hour, minute = int(time[:2]), int(time[3:5])
    rule = event.get("rrule")
    days = days_of(rule) if isinstance(rule, str) and rule else frozenset()
    summary = event.get("summary")
    summary = summary if isinstance(summary, str) else ""
    enabled = not summary.startswith(OFF_PREFIX)
    label = summary.removeprefix(OFF_PREFIX).strip()
    if label == NO_LABEL:
        label = ""
    label = "".join(c for c in label if ord(c) >= 32 and ord(c) != 127)[:MAX_LABEL].strip()
    volume, sound, ring, stamp, extra = _read_description(event.get("description") if isinstance(event.get("description"), str) else "")
    try:
        _parse_date(date)
    except ValueError as err:
        raise ValueError("its start is not a date") from err
    fields = Fields(label, hour, minute, days, enabled, volume, sound, ring, None if days else date)
    return Parsed(uid, fields, stamp, extra)


def read_events(events: Iterable[Mapping[str, Any]]) -> tuple[list[Parsed], list[tuple[str, str]]]:
    """The alarms of the events a calendar listed (an event of a series is listed once a day: the earliest is the series), and the events that
    are not alarms with the reason."""
    first: dict[str, Mapping[str, Any]] = {}
    for event in events:
        uid = event.get("uid")
        if not isinstance(uid, str):
            continue
        start = _stamp_text(event.get("start")) or ""
        kept = first.get(uid)
        if kept is None or start < (_stamp_text(kept.get("start")) or ""):
            first[uid] = event
    alarms: list[Parsed] = []
    skipped: list[tuple[str, str]] = []
    for uid, event in sorted(first.items()):
        try:
            alarms.append(read_event(uid, event))
        except ValueError as err:
            skipped.append((uid, str(err)))
    return alarms, skipped


def with_stamp(parsed: Parsed, stamp: Stamp | None) -> Parsed:
    return replace(parsed, stamp=stamp)
