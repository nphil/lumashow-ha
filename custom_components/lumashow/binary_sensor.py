"""Whether the Show is connected: the first thing to look at when a widget is not showing what it should."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LumaShowConfigEntry
from .entity import LumaShowEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaShowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([Connected(entry.runtime_data)])


class Connected(LumaShowEntity, BinarySensorEntity):
    """On while the channel to the Show is open and the protocol is agreed."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: Any) -> None:
        super().__init__(hub, "connected")

    @property
    def is_on(self) -> bool:
        return self.hub.connected
