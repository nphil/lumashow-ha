"""Module ``alarms``: the alarm clocks of several Shows kept in step, with one calendar of Home Assistant as the source of truth.

The integration is a **store with compare-and-swap and a relay** (docs/protocol.md, "alarms"; the Show's end is its ``alarm-sync`` crate and
``docs/alarms.md`` in the LumaShow repository):

- the alarms live in one Local Calendar, ``calendar.lumashow_alarms``, one event each (``alarms_codec`` is the format; a person may edit an event
  in Home Assistant's own calendar panel, and that is seen by every Show within a moment);
- every alarm has a ``rev`` that rises with every accepted write **and with every edit a person makes in the calendar**; a Show's ``put`` or
  ``delete`` is accepted only when its ``expect`` is the alarm's current ``rev``, otherwise it is answered with the alarm as it is now
  (``conflict``) and the Show merges and writes again. All the merging is the Show's;
- the whole set is pushed to every Show that shares alarms (``snapshot``: ``rev`` of the set, the zone, whether the calendar can be read, the
  alarms) after every change; a Show's beacons and answers are passed on to the others, naming the Show they are from (never a name the Show
  gave itself), and an answer is also fired as the event ``lumashow_alarm`` for automations (a backup ring on a speaker or a phone).

**Nothing is shared by default:** a Show gets alarms only when the person turned on *Share alarms* for it (an option of its entry). With none
sharing nothing here runs: no subscription, no timer.

The calendar is never created or changed by anything but a Show's write that the compare-and-swap accepted: this module adds no calendar,
helper or automation (the person adds the calendar once: Local Calendar, named "LumaShow alarms").
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .alarms_codec import (
    MAX_ALARMS,
    Fields,
    Parsed,
    Record,
    Stamp,
    Written,
    fields_from_json,
    plain_word,
    read_events,
    record_to_json,
    write_event,
)
from .const import DOMAIN, OPT_SHARE_ALARMS

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

MODULE: Final = "alarms"
VERSION: Final = 1
CALENDAR_ENTITY: Final = "calendar.lumashow_alarms"
EVENT_ALARM: Final = "lumashow_alarm"
# How far back and ahead the calendar is read: a series is listed once a day, a once-only alarm once. A once-only alarm whose day is more than
# this far gone leaves the Shows (it has rung), and one for a day further off than this appears on the Shows when it is this near.
WINDOW_BACK: Final = dt.timedelta(days=14)
WINDOW_AHEAD: Final = dt.timedelta(days=10)
RETRY_EVERY: Final = dt.timedelta(seconds=30)
RESUBSCRIBE_EVERY: Final = dt.timedelta(hours=6)
SAVE_DELAY: Final = 1.0
HOW: Final = ("stopped", "snoozed")


def _signature(fields: Fields) -> str:
    """What a hand edit changes: the fields of the alarm, nothing a person writes as a note."""
    return hashlib.sha1(json.dumps(fields.key(), default=list).encode()).hexdigest()


def _number(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError("a number is out of its range")
    return value


class AlarmsModule:
    """The owner of module ``alarms`` (registered in ``hass.data`` for the hubs to offer and to call)."""

    version = VERSION

    def __init__(self, hass: HomeAssistant, calendar_entity_id: str = CALENDAR_ENTITY) -> None:
        self.hass = hass
        self.calendar_entity_id = calendar_entity_id
        self._hubs: dict[str, Hub] = {}
        self._lock = asyncio.Lock()
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.alarms")
        self._meta: dict[str, Any] = {"rev": 0, "alarms": {}, "tz": None, "ready": None}
        self._loaded = False
        self._started = False
        self._records: dict[str, Record] = {}
        self._parsed: dict[str, Parsed] = {}
        self._reported: set[tuple[str, str]] = set()
        self._ready = False
        self._unsubscribe: CALLBACK_TYPE | None = None
        self._timers: list[CALLBACK_TYPE] = []
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_pending = False

    # -- what the hubs call --------------------------------------------------------------------------------------------------------

    @staticmethod
    def sharing(hub: Hub) -> bool:
        """Whether the person turned on *Share alarms* for this Show."""
        return bool(hub.options.get(OPT_SHARE_ALARMS))

    def offers(self, hub: Hub) -> bool:
        """The module is offered to a Show only when the person turned on *Share alarms* for it."""
        return self.sharing(hub)

    async def async_session(self, hub: Hub, up: bool) -> None:
        """A session of the module began (the Show agreed it) or ended."""
        key = hub.entry.entry_id
        if not up:
            self._hubs.pop(key, None)
            if not self._hubs:
                await self._async_stop()
            return
        if not self.sharing(hub):
            _LOGGER.debug("%s: alarms are not shared with this Show", hub.name)
            return
        self._hubs[key] = hub
        async with self._lock:
            await self._async_start()
        await self._push_snapshot(hub)

    async def async_message(self, hub: Hub, kind: str, body: Mapping[str, Any]) -> None:
        """A message of the Show."""
        if hub.entry.entry_id not in self._hubs:
            return
        if kind == "resync":
            await self._push_snapshot(hub)
        elif kind in ("put", "delete"):
            await self._write(hub, kind, body)
        elif kind == "beacon":
            await self._relay_beacon(hub, body)
        elif kind == "answered":
            await self._relay_answer(hub, body)
        else:
            _LOGGER.debug("%s: a message alarms.%s is not one this version knows", hub.name, kind)

    # -- the calendar --------------------------------------------------------------------------------------------------------------

    def _calendar(self) -> Any | None:
        component = self.hass.data.get(DATA_COMPONENT)
        return component.get_entity(self.calendar_entity_id) if component is not None else None

    def _window(self) -> tuple[dt.datetime, dt.datetime]:
        now = dt_util.now()
        return now - WINDOW_BACK, now + WINDOW_AHEAD

    async def _async_start(self) -> None:
        """Under the lock: reads what was kept, finds the calendar, reads it, and listens to it."""
        if self._started:
            return
        if not self._loaded:
            data = await self._store.async_load()
            if isinstance(data, dict):
                self._meta.update({k: data[k] for k in ("rev", "alarms", "tz", "ready") if k in data})
            self._loaded = True
        self._started = True
        self._timers.append(async_track_time_interval(self.hass, self._retry, RETRY_EVERY, cancel_on_shutdown=True))
        self._timers.append(async_track_time_interval(self.hass, self._resubscribe, RESUBSCRIBE_EVERY, cancel_on_shutdown=True))
        await self._attach()

    async def _async_stop(self) -> None:
        """No Show shares alarms any more: nothing listens and nothing is timed."""
        async with self._lock:
            self._started = False
            self._detach()
            for cancel in self._timers:
                cancel()
            self._timers.clear()
            self._ready = False
            if self._refresh_task is not None and not self._refresh_task.done():
                self._refresh_task.cancel()
            self._refresh_task = None
            self._refresh_pending = False

    def _detach(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def _attach(self) -> None:
        """Under the lock: listens to the calendar, if it is there, and reads it."""
        self._detach()
        entity = self._calendar()
        if entity is None:
            self._ready = False
            await self._absorb(None)
            return
        start, end = self._window()
        self._unsubscribe = entity.async_subscribe_events(start, end, self._pushed)
        await self._read(entity)

    async def _read(self, entity: Any | None = None) -> None:
        """Under the lock: reads the calendar as it is now."""
        entity = entity or self._calendar()
        if entity is None:
            self._ready = False
            await self._absorb(None)
            return
        start, end = self._window()
        try:
            events = await entity.async_get_events(self.hass, start, end)
        except HomeAssistantError as err:
            _LOGGER.debug("the alarms calendar could not be read: %s", err)
            self._ready = False
            await self._absorb(None)
            return
        await self._absorb([event.as_dict() for event in events])

    @callback
    def _pushed(self, events: list[Any] | None) -> None:
        """The calendar changed (a Show's write, or a person's edit in the calendar panel). What the push carries may be older than what the
        calendar holds now (it was read before a write that has come since), so it is only a reason to read again, under the lock."""
        if events is None or self._refresh_pending:
            return
        # Marked before the task is made: Home Assistant starts a task at once, and it clears the mark when it has the lock.
        self._refresh_pending = True
        self._refresh_task = self.hass.async_create_task(self._refresh(), name="lumashow alarms calendar")

    async def _refresh(self) -> None:
        async with self._lock:
            self._refresh_pending = False
            if self._started:
                await self._read()

    async def _retry(self, _now: dt.datetime) -> None:
        """Until the calendar is there (and while it is not readable), looks again."""
        async with self._lock:
            if self._started and (not self._ready or self._unsubscribe is None):
                await self._attach()

    async def _resubscribe(self, _now: dt.datetime) -> None:
        """The window the calendar is listened to moves with the days."""
        async with self._lock:
            if self._started:
                await self._attach()

    async def _absorb(self, events: list[Any] | None) -> None:
        """Under the lock: takes what the calendar holds as the alarms. ``None``: it cannot be read (nothing is believed gone)."""
        tz = str(self.hass.config.time_zone)
        changed = False
        if events is not None:
            self._ready = True
            parsed, skipped = read_events(events)
            for uid, why in skipped:
                if (uid, why) not in self._reported:
                    self._reported.add((uid, why))
                    _LOGGER.info("an event of the alarms calendar is not an alarm a Show can have (%s): %s", uid, why)
            alarms: dict[str, Any] = self._meta["alarms"]
            seen: dict[str, Parsed] = {}
            for item in parsed:
                seen[item.uid] = item
                signature = _signature(item.fields)
                meta = alarms.get(item.uid)
                if meta is None:
                    alarms[item.uid] = {"rev": 1, "sig": signature}
                    changed = True
                elif meta["sig"] != signature:
                    # A person edited it in the calendar panel: the alarm is a new revision, and no Show's stamp is on it.
                    meta["rev"] += 1
                    meta["sig"] = signature
                    changed = True
            for gone in set(self._records) - set(seen):
                alarms.pop(gone, None)
                changed = True
            self._parsed = seen
            self._records = {uid: Record(uid, alarms[uid]["rev"], item.fields, item.stamp or Stamp()) for uid, item in seen.items()}
        if (tz, self._ready) != (self._meta["tz"], self._meta["ready"]):
            self._meta["tz"], self._meta["ready"] = tz, self._ready
            changed = True
        if changed:
            self._meta["rev"] += 1
            self._save()
            await self._push_all()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self._meta, SAVE_DELAY)

    # -- what is sent --------------------------------------------------------------------------------------------------------------

    def _snapshot(self) -> dict[str, Any]:
        return {
            "rev": self._meta["rev"],
            "tz": str(self.hass.config.time_zone),
            "ready": self._ready,
            "alarms": [record_to_json(r) for _, r in sorted(self._records.items())],
        }

    async def _push_snapshot(self, hub: Hub) -> None:
        await hub.async_send_module(MODULE, "snapshot", self._snapshot())

    async def _push_all(self) -> None:
        snapshot = self._snapshot()
        for hub in list(self._hubs.values()):
            await hub.async_send_module(MODULE, "snapshot", snapshot)

    @staticmethod
    def _result(op: Any, **fields: Any) -> dict[str, Any]:
        return {"op": op, **fields}

    # -- a Show writes --------------------------------------------------------------------------------------------------------------

    async def _write(self, hub: Hub, kind: str, body: Mapping[str, Any]) -> None:
        """``put`` or ``delete``: accepted only against the alarm's current ``rev``."""
        op = body.get("op")
        try:
            op = _number(op, 0, 2**53)
            uid = body.get("uid")
            if not plain_word(uid, 64):
                raise ValueError("`uid` is not a plain word")
            expect = _number(body.get("expect"), 0, 2**31)
            fields = at = None
            if kind == "put":
                fields = fields_from_json(body.get("alarm"))
                at = _number(body.get("at"), 0, 2**40)
        except ValueError as err:
            await hub.async_send_module(
                MODULE, "result", self._result(op if isinstance(op, int) else 0, ok=False, code="bad_alarm", message=str(err))
            )
            return
        assert isinstance(uid, str)
        async with self._lock:
            # What a person did in the calendar panel a moment ago is counted before the Show's write is judged against it.
            await self._read()
            reply = await self._judged(hub, kind, op, uid, expect, fields, at)
        await hub.async_send_module(MODULE, "result", reply)

    async def _judged(self, hub: Hub, kind: str, op: int, uid: str, expect: int, fields: Fields | None, at: int | None) -> dict[str, Any]:
        entity = self._calendar()
        if entity is None or not self._ready:
            return self._result(op, ok=False, code="unavailable", message="the alarms calendar cannot be read")
        current = self._records.get(uid)
        current_rev = current.rev if current else 0
        if expect != current_rev:
            return self._result(
                op, ok=False, code="conflict", uid=uid, message="the alarm changed meanwhile", current=record_to_json(current) if current else None
            )
        try:
            if kind == "delete":
                if current is None:
                    return self._result(op, ok=True, uid=uid, rev=expect)
                await entity.async_delete_event(uid)
                rev = expect
                self._meta["alarms"].pop(uid, None)
                self._records.pop(uid, None)
                self._parsed.pop(uid, None)
            else:
                assert fields is not None and at is not None
                if current is None and len(self._records) >= MAX_ALARMS:
                    return self._result(op, ok=False, code="too_many", message=f"{MAX_ALARMS} alarms are the most")
                stamp = Stamp(hub.device_name, at)
                extra = self._parsed[uid].extra if uid in self._parsed else ""
                written = write_event(fields, stamp, extra, *self._first_day(fields))
                await self._send_to_calendar(entity, uid, written, create=current is None)
                rev = current_rev + 1
                self._meta["alarms"][uid] = {"rev": rev, "sig": _signature(fields)}
                self._records[uid] = Record(uid, rev, fields, stamp)
                self._parsed[uid] = Parsed(uid, fields, stamp, extra)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("%s: the alarms calendar refused a write of %s: %s", hub.name, uid, err)
            return self._result(op, ok=False, code="unavailable", message="the calendar refused the write")
        self._meta["rev"] += 1
        self._save()
        await self._push_all_except_result()
        return self._result(op, ok=True, uid=uid, rev=rev)

    async def _push_all_except_result(self) -> None:
        """Every Show hears of the change (the writer too: its copy is then exactly the calendar's)."""
        await self._push_all()

    @staticmethod
    def _first_day(fields: Fields) -> tuple[dt.date, int]:
        """The date an alarm's event starts on (the next day it is on, or its own date) and the zone's offset in seconds that day."""
        today = dt_util.now().date()
        if fields.days:
            day = next(today + dt.timedelta(days=n) for n in range(8) if (today + dt.timedelta(days=n)).weekday() in fields.days)
        elif fields.date is not None:
            day = dt.date.fromisoformat(fields.date)
        else:
            day = today
        local = dt.datetime.combine(day, dt.time(fields.hour, fields.minute), tzinfo=dt_util.get_default_time_zone())
        offset = local.utcoffset()
        return day, int(offset.total_seconds()) if offset else 0

    @staticmethod
    async def _send_to_calendar(entity: Any, uid: str, written: Written, *, create: bool) -> None:
        event: dict[str, Any] = {
            "summary": written.summary,
            "description": written.description,
            "dtstart": dt.datetime.fromisoformat(written.dtstart),
            "dtend": dt.datetime.fromisoformat(written.dtend),
            # Always said, so that an edit that makes a series a once-only alarm takes the rule away.
            "rrule": written.rrule,
        }
        if create:
            await entity.async_create_event(uid=uid, **event)
        else:
            await entity.async_update_event(uid, event)

    # -- what a Show tells the others -----------------------------------------------------------------------------------------------

    async def _relay_beacon(self, hub: Hub, body: Mapping[str, Any]) -> None:
        try:
            age = body.get("age")
            age = None if age is None else _number(age, 0, 2**32 - 1)
            prio = _number(body.get("prio", 0), 0, 255)
        except ValueError:
            return
        await self._relay(hub, "beacon", {"age": age, "prio": prio})

    async def _relay_answer(self, hub: Hub, body: Mapping[str, Any]) -> None:
        occ, how = body.get("occ"), body.get("how")
        if not plain_word(occ, 96) or how not in HOW:
            return
        await self._relay(hub, "answered", {"occ": occ, "how": how})
        # An automation can ring what the Shows cannot (a speaker, a phone) when nobody answered: it hears of the answers.
        self.hass.bus.async_fire(EVENT_ALARM, {"kind": "answered", "from": hub.device_name, "occ": occ, "how": how})

    async def _relay(self, hub: Hub, kind: str, fields: dict[str, Any]) -> None:
        """To every other Show that shares alarms, with the name of this one (the integration's, never one the Show gave)."""
        for other in list(self._hubs.values()):
            if other is not hub:
                await other.async_send_module(MODULE, kind, {"from": hub.device_name, **fields})


def module_of(hass: HomeAssistant) -> AlarmsModule | None:
    handler = hass.data.get(DOMAIN, {}).get("module_handlers", {}).get(MODULE)
    return handler if isinstance(handler, AlarmsModule) else None
