"""Fixtures: a fake Show on a real local socket, and a config entry that points at it."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumashow import hub as hub_module
from custom_components.lumashow.const import (
    CONF_ESPHOME_ENTRY,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_KEY,
    CONF_KEY_SOURCE,
    CONF_MAC,
    CONF_PORT,
    DOMAIN,
    KEY_SOURCE_MANUAL,
)

from .fake_show import FakeShow

PAIRING_KEY = bytes(range(32))
MAC = "aa:bb:cc:dd:ee:ff"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Lets the tests load the integration from the repository."""


@pytest.fixture(autouse=True)
def quick_reconnects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that makes the Show go away does not wait a second for the integration to look again."""
    monkeypatch.setattr(hub_module, "BACKOFF_MIN", 0.05)
    monkeypatch.setattr(hub_module, "BACKOFF_MAX", 0.2)


@pytest.fixture
async def show(socket_enabled: None) -> AsyncGenerator[FakeShow]:
    fake = FakeShow(PAIRING_KEY)
    await fake.start()
    yield fake
    await fake.stop()


def entry_data(port: int, key: bytes = PAIRING_KEY) -> dict[str, Any]:
    return {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: port,
        CONF_MAC: MAC,
        CONF_KEY: base64.b64encode(key).decode(),
        CONF_KEY_SOURCE: KEY_SOURCE_MANUAL,
        CONF_ESPHOME_ENTRY: None,
        CONF_FRIENDLY_NAME: "LumaShow abcdef",
    }


@pytest.fixture
def entry(hass: HomeAssistant, show: FakeShow) -> MockConfigEntry:
    """A config entry for the fake Show, already known to Home Assistant (a test sets its options, then `setup`s it)."""
    config = MockConfigEntry(
        domain=DOMAIN,
        title="Living Room Show",
        unique_id=MAC,
        data=entry_data(show.port),
        options={},
    )
    config.add_to_hass(hass)
    return config


@pytest.fixture
def rig(hass: HomeAssistant, show: FakeShow, entry: MockConfigEntry) -> Any:
    """A `Rig` (tests/rig.py): the entry, the fake Show, and the services a test looks at; a test starts it with what it shares."""
    from .rig import Rig

    return Rig(hass, show, entry)


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> hub_module.Hub:
    """Sets the entry up, and waits for the Show to have been told what it is given."""
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def settle(hass: HomeAssistant, seconds: float = 0.4) -> None:
    """Lets the channel and the debounce timers run."""
    await asyncio.sleep(seconds)
    await hass.async_block_till_done()
