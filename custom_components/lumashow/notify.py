"""A message for the Show's screen: ``notify.send_message`` on this entity shows it in the Show's notifications."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LumaShowConfigEntry
from .entity import LumaShowEntity
from .hub import Hub


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LumaShowConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ShowMessage(entry.runtime_data)])


class ShowMessage(LumaShowEntity, NotifyEntity):
    """The Show's notifications."""

    def __init__(self, hub: Hub) -> None:
        super().__init__(hub, "message")

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        await self.hub.async_notify(title, message)
