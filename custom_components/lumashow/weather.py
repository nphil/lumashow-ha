"""Module ``weather``: a weather entity's reading and forecasts, for a device that draws the weather (a LumaTV's Home).

The person picks the weather entity (Pirate Weather, a weather station, Met.no, ...) **on the device** from the list this module sends, and may
take the choice back. Nothing is sent until the person turned on *Send a weather entity* for that device (an option of its entry; off by
default) and a choice was made. The device receives no other entity: this module reads the one entity's state and attributes and asks Home
Assistant for its forecasts (``weather.get_forecasts``, hourly and daily), and sends them as one ``snapshot``, again when the entity changes
(debounced) and every 15 minutes, because an integration whose forecasts change without the state changing would otherwise never be sent again.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval

from .const import OPT_SHARE_WEATHER, OPT_WEATHER_ENTITY

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

MODULE: Final = "weather"
VERSION: Final = 1
# A snapshot is sent at least this often, and a change of the entity waits this long for the next ones (a station updates five attributes in a row).
EVERY: Final = 15 * 60.0
DEBOUNCE: Final = 5.0
# The most hours and days sent (a message is at most 48 KiB).
MAX_HOURS: Final = 48
MAX_DAYS: Final = 10
# The attributes of the reading that are sent, as the weather integration names them.
READING: Final = (
    "temperature",
    "apparent_temperature",
    "dew_point",
    "humidity",
    "pressure",
    "wind_speed",
    "wind_gust_speed",
    "wind_bearing",
    "cloud_coverage",
    "uv_index",
    "visibility",
    "ozone",
    "temperature_unit",
    "pressure_unit",
    "wind_speed_unit",
    "visibility_unit",
    "precipitation_unit",
)
# The fields of a forecast entry that are sent.
ENTRY: Final = (
    "datetime",
    "condition",
    "temperature",
    "templow",
    "apparent_temperature",
    "precipitation",
    "precipitation_probability",
    "humidity",
    "wind_speed",
    "wind_gust_speed",
    "wind_bearing",
    "cloud_coverage",
    "uv_index",
    "is_daytime",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def reading_of(state: State) -> dict[str, Any]:
    """The attributes of a weather entity's state that the device uses, as numbers and words."""
    out: dict[str, Any] = {}
    for key in READING:
        value = state.attributes.get(key)
        if isinstance(value, str) and key.endswith("_unit"):
            out[key] = value
        elif (number := _number(value)) is not None:
            out[key] = number
    return out


def entries_of(forecast: Any, limit: int) -> list[dict[str, Any]]:
    """A forecast as Home Assistant gives it (a list of mappings) cut to the fields and the length the device uses."""
    out: list[dict[str, Any]] = []
    if not isinstance(forecast, list):
        return out
    for item in forecast[:limit]:
        if not isinstance(item, Mapping) or not isinstance(item.get("datetime"), str):
            continue
        entry: dict[str, Any] = {"datetime": item["datetime"]}
        for key in ENTRY[1:]:
            value = item.get(key)
            if (key == "condition" and isinstance(value, str)) or (key == "is_daytime" and isinstance(value, bool)):
                entry[key] = value
            elif (number := _number(value)) is not None:
                entry[key] = number
        out.append(entry)
    return out


class WeatherModule:
    """The owner of module ``weather`` (registered in ``hass.data`` for the hubs to offer and to call)."""

    version = VERSION

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._watches: dict[str, _Watch] = {}

    @staticmethod
    def sharing(hub: Hub) -> bool:
        return bool(hub.options.get(OPT_SHARE_WEATHER))

    def offers(self, hub: Hub) -> bool:
        return self.sharing(hub)

    async def async_session(self, hub: Hub, up: bool) -> None:
        key = hub.entry.entry_id
        if not up:
            watch = self._watches.pop(key, None)
            if watch:
                watch.stop()
            return
        if not self.sharing(hub):
            return
        watch = self._watches.get(key)
        if watch is None:
            watch = self._watches[key] = _Watch(self.hass, hub)
        await watch.push_list()
        await watch.start()

    async def async_message(self, hub: Hub, kind: str, body: Mapping[str, Any]) -> None:
        watch = self._watches.get(hub.entry.entry_id)
        if watch is None:
            return
        if kind == "list":
            await watch.push_list()
        elif kind == "select":
            await watch.select(body.get("entity"))
        elif kind == "refresh":
            await watch.push_snapshot()
        else:
            _LOGGER.debug("%s: a message weather.%s is not one this version knows", hub.name, kind)


class _Watch:
    """One device's weather: which entity, and when it was last sent."""

    def __init__(self, hass: HomeAssistant, hub: Hub) -> None:
        self.hass = hass
        self.hub = hub
        self._unsub: list[Any] = []
        self._due: Any = None
        self.clock = time.monotonic

    @property
    def entity_id(self) -> str | None:
        value = self.hub.options.get(OPT_WEATHER_ENTITY)
        return value if isinstance(value, str) and value.startswith("weather.") else None

    def stop(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        if self._due:
            self._due()
            self._due = None

    async def start(self) -> None:
        self.stop()
        entity_id = self.entity_id
        if not entity_id:
            return
        self._unsub.append(async_track_state_change_event(self.hass, [entity_id], self._changed))
        self._unsub.append(async_track_time_interval(self.hass, self._tick, _interval()))
        await self.push_snapshot()

    def choices(self) -> list[dict[str, str]]:
        registry = er.async_get(self.hass)
        out = []
        for state in self.hass.states.async_all("weather"):
            entry = registry.async_get(state.entity_id)
            if entry and (entry.hidden_by or entry.disabled_by):
                continue
            out.append({"id": state.entity_id, "name": state.name})
        return sorted(out, key=lambda c: c["name"].lower())

    async def push_list(self) -> None:
        await self.hub.async_send_module(MODULE, "list", {"entities": self.choices(), "selected": self.entity_id})

    async def select(self, entity: Any) -> None:
        """The device chose an entity (or none: ``null`` takes the choice back)."""
        if entity is not None and (not isinstance(entity, str) or not any(c["id"] == entity for c in self.choices())):
            await self.hub.async_send_module(MODULE, "failed", {"op": "select", "code": "not_found", "message": "that is not a weather entity"})
            return
        options = {**self.hub.entry.options, OPT_WEATHER_ENTITY: entity}
        self.hass.config_entries.async_update_entry(self.hub.entry, options=options)
        await self.start()
        await self.push_list()

    async def forecast(self, entity_id: str, kind: str) -> list[dict[str, Any]]:
        """``weather.get_forecasts`` of one kind; an integration that has no such forecast gives an empty list."""
        try:
            reply = await self.hass.services.async_call(
                "weather", "get_forecasts", {ATTR_ENTITY_ID: entity_id, "type": kind}, blocking=True, return_response=True
            )
        except Exception as err:
            _LOGGER.debug("%s: no %s forecast from %s: %s", self.hub.name, kind, entity_id, err)
            return []
        data = (reply or {}).get(entity_id) or {}
        return entries_of(data.get("forecast"), MAX_HOURS if kind == "hourly" else MAX_DAYS)

    async def push_snapshot(self) -> None:
        entity_id = self.entity_id
        if not entity_id:
            return
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            await self.hub.async_send_module(MODULE, "snapshot", {"entity": entity_id, "available": False, "at": time.time()})
            return
        hourly = await self.forecast(entity_id, "hourly")
        daily = await self.forecast(entity_id, "daily")
        await self.hub.async_send_module(
            MODULE,
            "snapshot",
            {
                "entity": entity_id,
                "available": True,
                "name": state.name,
                "condition": state.state,
                "reading": reading_of(state),
                "hourly": hourly,
                "daily": daily,
                "at": time.time(),
            },
        )

    @callback
    def _changed(self, event: Event) -> None:
        if self._due:
            return
        from homeassistant.helpers.event import async_call_later

        def fire(_now: Any) -> None:
            self._due = None
            self.hub.spawn_module_call(self.push_snapshot, "lumashow module weather snapshot")

        self._due = async_call_later(self.hass, DEBOUNCE, fire)

    @callback
    def _tick(self, _now: Any) -> None:
        self.hub.spawn_module_call(self.push_snapshot, "lumashow module weather snapshot")


def _interval() -> Any:
    from datetime import timedelta

    return timedelta(seconds=EVERY)
