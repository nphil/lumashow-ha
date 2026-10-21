"""Adding a Show: found by itself, the key taken from its ESPHome device, asked only when there is none."""

from __future__ import annotations

import base64
from ipaddress import ip_address
from typing import Any

from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lumashow.const import (
    CONF_ESPHOME_ENTRY,
    CONF_HOST,
    CONF_KEY,
    CONF_KEY_SOURCE,
    CONF_MAC,
    CONF_PORT,
    DOMAIN,
    KEY_SOURCE_ESPHOME,
    KEY_SOURCE_MANUAL,
)

from .conftest import MAC, PAIRING_KEY, entry_data, setup
from .fake_show import FakeShow

GOOD = base64.b64encode(PAIRING_KEY).decode()
OTHER = base64.b64encode(bytes(range(1, 33))).decode()


def discovery(show: FakeShow, **properties: str) -> ZeroconfServiceInfo:
    props = {"mac": "aabbccddeeff", "proto": "1", "friendly_name": "LumaShow abcdef", "project_name": "LumaShow.Echo Show 5", **properties}
    return ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        port=show.port,
        hostname="lumashow-abcdef.local.",
        type="_lumashow._tcp.local.",
        name="lumashow-abcdef._lumashow._tcp.local.",
        properties=props,
    )


def esphome_entry(hass: HomeAssistant, key: str | None = GOOD, unique_id: str = MAC) -> MockConfigEntry:
    entry = MockConfigEntry(domain="esphome", unique_id=unique_id, title="lumashow-abcdef", data={"noise_psk": key} if key else {})
    entry.add_to_hass(hass)
    return entry


async def discover(hass: HomeAssistant, show: FakeShow, **properties: str) -> Any:
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=discovery(show, **properties))


async def test_a_discovered_show_with_an_esphome_entry_is_added_without_typing_anything(hass: HomeAssistant, show: FakeShow) -> None:
    esphome = esphome_entry(hass)
    result = await discover(hass, show)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "discovery_confirm"
    assert result["description_placeholders"]["name"] == "LumaShow abcdef"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "LumaShow abcdef"
    assert result["data"] == {
        CONF_HOST: "127.0.0.1",
        CONF_PORT: show.port,
        CONF_MAC: MAC,
        CONF_KEY: GOOD,
        CONF_KEY_SOURCE: KEY_SOURCE_ESPHOME,
        CONF_ESPHOME_ENTRY: esphome.entry_id,
        "friendly_name": "LumaShow abcdef",
    }
    assert result["result"].unique_id == MAC
    # Nothing is shared yet: the options are empty.
    assert result["options"] == {}


async def test_a_show_with_no_esphome_entry_asks_for_its_key_and_says_what_is_wrong_with_a_wrong_one(hass: HomeAssistant, show: FakeShow) -> None:
    result = await discover(hass, show)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "key"
    wrong = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: OTHER})
    assert wrong["type"] is FlowResultType.FORM and wrong["errors"] == {"base": "invalid_auth"}
    malformed = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: "not a key"})
    assert malformed["errors"] == {"base": "invalid_key"}
    done = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: f"  {GOOD}  "})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert done["data"][CONF_KEY] == GOOD and done["data"][CONF_KEY_SOURCE] == KEY_SOURCE_MANUAL and done["data"][CONF_ESPHOME_ENTRY] is None


async def test_an_esphome_key_that_does_not_open_the_channel_is_not_used_and_the_person_is_asked(hass: HomeAssistant, show: FakeShow) -> None:
    esphome_entry(hass, key=OTHER)
    result = await discover(hass, show)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "key"


async def test_an_esphome_entry_of_another_device_is_not_used(hass: HomeAssistant, show: FakeShow) -> None:
    esphome_entry(hass, unique_id="11:22:33:44:55:66")
    result = await discover(hass, show)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "key"


async def test_a_show_that_cannot_be_reached_after_discovery_ends_the_flow(hass: HomeAssistant, show: FakeShow, socket_enabled: None) -> None:
    esphome_entry(hass)
    info = discovery(show)
    info.port = 1
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


async def test_something_that_announces_no_address_is_not_a_lumashow(hass: HomeAssistant, show: FakeShow) -> None:
    result = await discover(hass, show, mac="")
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_lumashow"


async def test_a_show_that_is_already_added_is_not_added_twice_and_its_new_address_is_kept(hass: HomeAssistant, show: FakeShow) -> None:
    existing = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="Show", data=entry_data(1))
    existing.add_to_hass(hass)
    result = await discover(hass, show)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert existing.data[CONF_PORT] == show.port and existing.data[CONF_HOST] == "127.0.0.1"


async def test_adding_by_address_finds_out_what_the_show_is_and_uses_its_esphome_key(hass: HomeAssistant, show: FakeShow) -> None:
    esphome_entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: " 127.0.0.1 ", CONF_PORT: show.port})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAC] == MAC and result["data"][CONF_KEY_SOURCE] == KEY_SOURCE_ESPHOME
    assert result["title"] == "lumashow-abcdef"


async def test_adding_by_address_with_nothing_there_says_so(hass: HomeAssistant, socket_enabled: None) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "cannot_connect"}


async def test_reauth_takes_the_new_esphome_key_when_there_is_one(hass: HomeAssistant, show: FakeShow) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="Show", data=entry_data(show.port, bytes(range(1, 33))))
    entry.add_to_hass(hass)
    esphome_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_KEY] == GOOD and entry.data[CONF_KEY_SOURCE] == KEY_SOURCE_ESPHOME


async def test_reauth_asks_for_the_key_when_there_is_no_better_one(hass: HomeAssistant, show: FakeShow) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=MAC, title="Show", data=entry_data(show.port, bytes(range(1, 33))))
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reauth_confirm"
    wrong = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: OTHER})
    assert wrong["errors"] == {"base": "invalid_auth"}
    done = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: GOOD})
    assert done["type"] is FlowResultType.ABORT and done["reason"] == "reauth_successful"
    assert entry.data[CONF_KEY] == GOOD and entry.data[CONF_KEY_SOURCE] == KEY_SOURCE_MANUAL


async def test_the_hub_follows_a_new_esphome_key_by_itself_and_starts_no_reauth(hass: HomeAssistant, show: FakeShow) -> None:
    """The Show was paired again: Home Assistant's ESPHome entry has the new key, and the integration takes it from there."""
    entry = MockConfigEntry(domain=DOMAIN, title="Show", unique_id=MAC, data=entry_data(show.port, bytes(range(1, 33))), options={})
    esphome = esphome_entry(hass)
    hub = await setup(hass, entry)
    session = await show.wait_session()
    await session.wait_kind("states", "snapshot")
    assert hub.connected and entry.data[CONF_KEY] == GOOD and entry.data[CONF_ESPHOME_ENTRY] == esphome.entry_id
    assert not [f for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN) if f["context"]["source"] == "reauth"]
