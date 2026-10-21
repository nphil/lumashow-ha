"""The base of the integration's entities: one device per Show, updated when its hub says something changed."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_UPDATE
from .hub import Hub


class LumaShowEntity(Entity):
    """An entity of a Show's device. The ESPHome entities of the same Show share the device (they share its hardware address)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: Hub, key: str) -> None:
        self.hub = hub
        self._attr_unique_id = f"{hub.mac}_{key}"
        self._attr_translation_key = key

    @property
    def device_info(self) -> DeviceInfo:
        device = self.hub.device
        return DeviceInfo(
            identifiers={(DOMAIN, self.hub.mac)},
            connections={(dr.CONNECTION_NETWORK_MAC, self.hub.mac)},
            manufacturer=str(device.get("manufacturer") or "LumaShow"),
            model=str(device.get("model") or "") or None,
            name=self.hub.name,
            sw_version=str(device.get("sw_version") or "") or None,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATE.format(self.hub.entry.entry_id), self._hub_changed))

    @callback
    def _hub_changed(self) -> None:
        self.async_write_ha_state()
