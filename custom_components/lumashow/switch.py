"""*Pause control*: while it is on, the Show is told it may do nothing and every action it asks for is refused."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LumaShowConfigEntry
from .entity import LumaShowEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaShowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([PauseControl(entry.runtime_data)])


class PauseControl(LumaShowEntity, SwitchEntity):
    """The person's brake: it is remembered across restarts (the hub keeps it)."""

    def __init__(self, hub: Any) -> None:
        super().__init__(hub, "pause_control")

    @property
    def is_on(self) -> bool:
        return self.hub.paused

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.hub.set_paused(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.hub.set_paused(False)
