"""Diagnostic sensors of a Show: how many entities are shared, when something was last pushed, and how many actions were done."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
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
    hub = entry.runtime_data
    async_add_entities([EntitiesShared(hub), LastPush(hub), Actions(hub)])


class EntitiesShared(LumaShowEntity, SensorEntity):
    """How many entities the Show is given."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hub: Any) -> None:
        super().__init__(hub, "entities_shared")

    @property
    def native_value(self) -> int:
        return self.hub.shared_count

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"controllable": self.hub.controllable_count, "control_paused": self.hub.paused}


class LastPush(LumaShowEntity, SensorEntity):
    """When Home Assistant last pushed to the Show (to the minute: the recorder is not written to for every state)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hub: Any) -> None:
        super().__init__(hub, "last_push")

    @property
    def native_value(self) -> datetime | None:
        pushed = self.hub.last_push
        return pushed.replace(second=0, microsecond=0) if pushed else None


class Actions(LumaShowEntity, SensorEntity):
    """Actions the Show asked for and Home Assistant carried out (the refused ones are counted in an attribute)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, hub: Any) -> None:
        super().__init__(hub, "actions")

    @property
    def native_value(self) -> int:
        return self.hub.actions

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"refused": self.hub.refused, "last_action": self.hub.last_action}
