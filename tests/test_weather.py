"""Module ``weather``: the reading and the forecasts of the one entity the device chose, and nothing else."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumashow.const import OPT_SHARE_WEATHER, OPT_WEATHER_ENTITY
from custom_components.lumashow.weather import MAX_DAYS, MAX_HOURS, WeatherModule, entries_of, reading_of

ENTITY = "weather.pirate_weather"


class FakeHub:
    def __init__(self, hass: HomeAssistant, *, sharing: bool = True, entity: str | None = ENTITY) -> None:
        self.name = "Living Room TV"
        self.entry = MockConfigEntry(
            domain="lumashow", data={}, options={OPT_SHARE_WEATHER: sharing, OPT_WEATHER_ENTITY: entity}, entry_id="entry-tv"
        )
        self.entry.add_to_hass(hass)
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.calls: list[str] = []

    @property
    def options(self) -> Any:
        return self.entry.options

    async def async_send_module(self, module: str, kind: str, fields: Any) -> bool:
        assert module == "weather"
        self.sent.append((kind, dict(fields)))
        return True

    def spawn_module_call(self, call: Any, name: str) -> None:
        self.calls.append(name)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [f for k, f in self.sent if k == kind]


def _hours(n: int) -> list[dict[str, Any]]:
    return [
        {
            "datetime": f"2026-10-30T{h % 24:02d}:00:00+00:00",
            "condition": "rainy",
            "temperature": 60.0 + h,
            "precipitation_probability": 40,
            "wind_speed": 7.5,
            "secret": "x",
        }
        for h in range(n)
    ]


async def _register_forecasts(hass: HomeAssistant, calls: list[dict[str, Any]]) -> None:
    async def get_forecasts(call: ServiceCall) -> dict[str, Any]:
        calls.append(dict(call.data))
        kind = call.data["type"]
        if kind == "twice_daily":
            raise ValueError("not supported")
        forecast = (
            _hours(80)
            if kind == "hourly"
            else [{"datetime": "2026-10-30", "condition": "sunny", "temperature": 71, "templow": 51, "is_daytime": True}]
        )
        return {ENTITY: {"forecast": forecast}}

    hass.services.async_register("weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY)


def test_the_reading_keeps_the_numbers_and_units_the_device_uses_and_nothing_else() -> None:
    state = SimpleNamespace(
        attributes={
            "temperature": 61.2,
            "humidity": 53,
            "wind_speed": 6.0,
            "temperature_unit": "°F",
            "uv_index": True,
            "friendly_name": "Pirate",
            "attribution": "x",
            "pressure": "high",
        }
    )
    assert reading_of(state) == {"temperature": 61.2, "humidity": 53.0, "wind_speed": 6.0, "temperature_unit": "°F"}


def test_a_forecast_is_cut_to_its_fields_and_its_length_and_a_bad_entry_is_dropped() -> None:
    cut = entries_of([*_hours(80), {"condition": "x"}, "nope"], MAX_HOURS)
    assert len(cut) == MAX_HOURS
    assert cut[0] == {
        "datetime": "2026-10-30T00:00:00+00:00",
        "condition": "rainy",
        "temperature": 60.0,
        "precipitation_probability": 40.0,
        "wind_speed": 7.5,
    }
    assert entries_of(None, 5) == [] and entries_of([{"datetime": 5}], 5) == []
    assert MAX_DAYS == 10


@pytest.mark.parametrize("sharing", [False, True])
def test_the_module_is_offered_only_when_the_person_turned_it_on(hass: HomeAssistant, sharing: bool) -> None:
    hub = FakeHub(hass, sharing=sharing)
    assert WeatherModule(hass).offers(hub) is sharing  # type: ignore[arg-type]


async def test_a_session_sends_the_list_and_the_chosen_entitys_snapshot(hass: HomeAssistant) -> None:
    calls: list[dict[str, Any]] = []
    await _register_forecasts(hass, calls)
    hass.states.async_set(ENTITY, "rainy", {"friendly_name": "Pirate Weather", "temperature": 61.0, "humidity": 80, "temperature_unit": "°F"})
    hass.states.async_set("weather.home", "sunny", {"friendly_name": "Home"})
    hass.states.async_set("sensor.not_weather", "1")
    hub = FakeHub(hass)
    module = WeatherModule(hass)
    await module.async_session(hub, True)  # type: ignore[arg-type]
    listed = hub.of("list")[0]
    assert [e["id"] for e in listed["entities"]] == ["weather.home", ENTITY] and listed["selected"] == ENTITY, (
        "weather entities only, the choice named"
    )
    snap = hub.of("snapshot")[0]
    assert snap["entity"] == ENTITY and snap["available"] and snap["condition"] == "rainy" and snap["name"] == "Pirate Weather"
    assert snap["reading"] == {"temperature": 61.0, "humidity": 80.0, "temperature_unit": "°F"}
    assert len(snap["hourly"]) == MAX_HOURS and snap["daily"][0]["templow"] == 51.0
    assert {c["type"] for c in calls} == {"hourly", "daily"}
    await module.async_session(hub, False)  # type: ignore[arg-type]


async def test_an_entity_that_is_not_there_says_so_and_a_forecast_it_cannot_give_is_empty(hass: HomeAssistant) -> None:
    calls: list[dict[str, Any]] = []
    await _register_forecasts(hass, calls)
    hub = FakeHub(hass)
    module = WeatherModule(hass)
    await module.async_session(hub, True)  # type: ignore[arg-type]
    assert hub.of("snapshot")[0] == {"entity": ENTITY, "available": False, "at": hub.of("snapshot")[0]["at"]}
    hass.states.async_set(ENTITY, "unavailable")
    await module._watches["entry-tv"].push_snapshot()
    assert hub.of("snapshot")[-1]["available"] is False
    assert await module._watches["entry-tv"].forecast(ENTITY, "twice_daily") == []
    await module.async_session(hub, False)  # type: ignore[arg-type]


async def test_the_device_chooses_an_entity_from_the_list_and_can_take_the_choice_back(hass: HomeAssistant) -> None:
    calls: list[dict[str, Any]] = []
    await _register_forecasts(hass, calls)
    hass.states.async_set(ENTITY, "rainy", {"temperature": 61.0})
    hass.states.async_set("weather.home", "sunny", {})
    hub = FakeHub(hass, entity=None)
    module = WeatherModule(hass)
    await module.async_session(hub, True)  # type: ignore[arg-type]
    assert hub.of("snapshot") == [], "nothing is sent before a choice"
    await module.async_message(hub, "select", {"entity": "sensor.not_weather"})  # type: ignore[arg-type]
    assert hub.of("failed")[0]["code"] == "not_found" and hub.options.get(OPT_WEATHER_ENTITY) is None
    await module.async_message(hub, "select", {"entity": ENTITY})  # type: ignore[arg-type]
    assert hub.options[OPT_WEATHER_ENTITY] == ENTITY and hub.of("snapshot")[-1]["entity"] == ENTITY and hub.of("list")[-1]["selected"] == ENTITY
    before = len(hub.of("snapshot"))
    await module.async_message(hub, "select", {"entity": None})  # type: ignore[arg-type]
    assert hub.options[OPT_WEATHER_ENTITY] is None and len(hub.of("snapshot")) == before
    await module.async_session(hub, False)  # type: ignore[arg-type]


async def test_a_change_of_the_entity_is_sent_once_after_the_wait_and_a_session_that_ends_stops_the_watch(hass: HomeAssistant) -> None:
    calls: list[dict[str, Any]] = []
    await _register_forecasts(hass, calls)
    hass.states.async_set(ENTITY, "rainy", {"temperature": 61.0})
    hub = FakeHub(hass)
    module = WeatherModule(hass)
    await module.async_session(hub, True)  # type: ignore[arg-type]
    watch = module._watches["entry-tv"]
    hass.states.async_set(ENTITY, "rainy", {"temperature": 62.0})
    hass.states.async_set(ENTITY, "cloudy", {"temperature": 63.0})
    await hass.async_block_till_done()
    assert watch._due is not None, "the change waits for the next ones"
    await module.async_session(hub, False)  # type: ignore[arg-type]
    assert watch._due is None and watch._unsub == []
