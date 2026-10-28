"""LumaShow: Home Assistant's end of the channel to a LumaShow device (an Echo Show or another).

Home Assistant pushes to each Show only what the person chose to share with it, and the Show asks for actions that this integration decides
(docs/protocol.md). Nothing is shared until the person adds something: Settings, Devices & services, LumaShow, Configure.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from .alarms import MODULE as ALARMS
from .alarms import AlarmsModule
from .camera_events import MODULE as CAMERA_EVENTS
from .camera_events import CameraEventsModule
from .camera_live import MODULE as CAMERA_LIVE
from .camera_live import CameraLiveModule
from .hub import Hub, module_handlers

PLATFORMS = [Platform.BINARY_SENSOR, Platform.NOTIFY, Platform.SENSOR, Platform.SWITCH]

type LumaShowConfigEntry = ConfigEntry[Hub]


async def async_setup_entry(hass: HomeAssistant, entry: LumaShowConfigEntry) -> bool:
    """Starts the hub (it connects by itself, and again whenever the Show is not there) and the entities."""
    # The owner of module `alarms` (the alarm clocks of several Shows): one for every Show; it runs only while a Show shares alarms.
    module_handlers(hass).setdefault(ALARMS, AlarmsModule(hass))
    # The owner of module `camera_events` (what a shared camera's detectors see, for a pop-up): runs only while a device shares them.
    module_handlers(hass).setdefault(CAMERA_EVENTS, CameraEventsModule(hass))
    # The owner of module `camera_live` (a shared camera's stream for a device with a decoder, without the camera's address): offered with the events.
    module_handlers(hass).setdefault(CAMERA_LIVE, CameraLiveModule(hass))
    hub = Hub(hass, entry)
    await hub.async_start()
    entry.runtime_data = hub

    async def _shutdown(_: Event) -> None:
        # Home Assistant does not unload entries when it stops: the Show is told, and drops everything at once.
        await hub.async_stop("shutdown")

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LumaShowConfigEntry) -> bool:
    """Unloading (disabling the entry, reloading it, removing it) cuts the Show off: it is told, and drops everything at once."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop("disabled" if entry.disabled_by else "unloaded")
    return unloaded
