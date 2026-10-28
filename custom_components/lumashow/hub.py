"""The runtime of one config entry (one Show): the channel to it, what is pushed over it, and every decision about what it asks.

**The Show asks, this decides.** An ``actions.call`` is checked here, in this order: control is not paused, the rate limit (10 calls in 10
seconds), the shape of the call (1-16 entities of one domain, a verb, plain values), the verb is in the fixed table of its domain
(``policy.VERBS``), every entity is shared with this Show and the person lets it act on them, the values are in range, and for a sensitive
verb the person enabled it for that entity and the call came from a touch, confirmed, with the PIN. What is refused is logged and counted;
what is done is in the logbook as ``<Show>: <what>`` with the entity, counted, and fired as ``lumashow_action``.

**The Show sees what was shared, and nothing else.** The snapshot carries the shared entities (their names, their areas, the allowed
attributes of their domain, and the verbs the Show may use on each) and the names of those areas only. Changes follow as updates.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import instance_id
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store

from .alarms_codec import plain_word
from .attributes import filter_attributes
from .channel import AuthError, Channel, ChannelError
from .const import (
    BACKOFF_MAX,
    BACKOFF_MIN,
    CALL_TIMEOUT,
    CONF_ESPHOME_ENTRY,
    CONF_HOST,
    CONF_KEY,
    CONF_MAC,
    CONF_PORT,
    CONNECT_TIMEOUT,
    DOMAIN,
    EVENT_ACTION,
    LAST_PUSH_EVERY,
    MAX_CALL_ENTITIES,
    OPT_ENTITIES,
    OPT_FORWARD_NOTIFICATIONS,
    OPT_PIN_HASH,
    OPT_PIN_SALT,
    OPT_SHARE_STREAMS,
    PIN_FAILURES,
    PIN_LOCKOUT,
    PIN_WINDOW,
    PING_EVERY,
    PUSH_DEBOUNCE,
    RATE_CALLS,
    RATE_WINDOW,
    SIGNAL_UPDATE,
    SILENT_AFTER,
    SNAPSHOT_URL_LIFETIME,
    SNAPSHOT_URL_REFRESH,
    VERSION,
)
from .pin import pin_ok
from .policy import BadArgs, describe, domain_of, is_sensitive, validate_args, verb_lists, verbs_of
from .protocol import MAX_PART, MODULES, PROTO, Message, ProtocolError, message
from .selection import Shared, area_names, control_flags, listed, resolve

_LOGGER = logging.getLogger(__name__)

ORIGINS = ("touch", "plugin", "voice", "automation")
# How long a refusal's logbook line is held back after the last one (every refusal is still logged and counted).
REFUSAL_LOG_EVERY = 10.0


class Refusal(Exception):
    """A call that is not carried out: the code the Show is told, and words for the log."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ModuleHandler(Protocol):
    """The owner of a module this integration does not implement itself (``alarms``, later): registered in ``hass.data``."""

    version: int

    def offers(self, hub: Hub) -> bool:
        """Whether the module is offered to this Show in ``hello`` (the person's choice for that Show: nothing is shared by default)."""

    async def async_session(self, hub: Hub, up: bool) -> None:
        """A session of the module began (``True``) or ended."""

    async def async_message(self, hub: Hub, kind: str, body: Mapping[str, Any]) -> None:
        """A message of the module arrived."""


def module_handlers(hass: HomeAssistant) -> dict[str, ModuleHandler]:
    """The registered owners of modules, by name (an empty dict in version 0.1)."""
    return hass.data.setdefault(DOMAIN, {}).setdefault("module_handlers", {})


def _signature(state: str, attrs: Mapping[str, Any], changed: float | None) -> str:
    return json.dumps([state, attrs, changed], sort_keys=True, separators=(",", ":"))


def _standing_notifications(hass: HomeAssistant) -> Mapping[str, Mapping[str, Any]]:
    """The persistent notifications standing now. Home Assistant has no public call for them; its own websocket command reads this one."""
    from homeassistant.components import persistent_notification

    getter = getattr(persistent_notification, "_async_get_or_create_notifications", None)
    return getter(hass) if getter else {}


class Hub:
    """One Show: the channel, what is pushed, what is asked."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.mac = dr.format_mac(entry.data[CONF_MAC])
        self.store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        # What the person sees on the Show's device.
        self.connected = False
        self.device: dict[str, Any] = {}
        self.agreed: dict[str, int] = {}
        self.paused = False
        self.actions = 0
        self.refused = 0
        self.last_action: str | None = None
        self.last_push: datetime | None = None
        self.rev = 0
        # Runtime.
        self.clock: Callable[[], float] = time.monotonic
        self._channel: Channel | None = None
        self._task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unsubs: list[Callable[[], None]] = []
        self._state_unsub: Callable[[], None] | None = None
        self._allowed: dict[str, Shared] = {}
        self._sent: dict[str, str] = {}
        self._structure: str = ""
        self._dirty: set[str] = set()
        self._flush_handle: asyncio.TimerHandle | None = None
        self._resnap_handle: asyncio.TimerHandle | None = None
        self._camera_handle: asyncio.TimerHandle | None = None
        self._last_push_signal = 0.0
        self._rate: deque[float] = deque()
        self._pin_failures: deque[float] = deque()
        self._locked_until = 0.0
        self._last_refusal_log = -REFUSAL_LOG_EVERY
        self._ping = 0
        self._stopping = False
        self._reauth_started = False

    # -- what an entity shows ----------------------------------------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.entry.title

    @property
    def device_name(self) -> str:
        """The Show's own name for itself (``lumashow-a1b2c3``, from its welcome): what a module stamps and relays the Show by."""
        name = self.device.get("name")
        if plain_word(name, 40):
            return str(name)
        return "".join(c if c.isascii() and c.isalnum() else "-" for c in self.entry.title)[:40] or DOMAIN

    @property
    def options(self) -> Mapping[str, Any]:
        return self.entry.options

    @property
    def shared_count(self) -> int:
        return len(self._allowed)

    @property
    def controllable_count(self) -> int:
        return sum(1 for entity_id in self._allowed if any(self._verbs(entity_id)))

    def signal(self) -> None:
        """Tells the entities something changed."""
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    # -- start and stop -------------------------------------------------------------------------------------------------------------

    async def async_start(self) -> None:
        """Loads what was kept, listens to the registries and the states of what is shared, and starts to connect."""
        data = await self.store.async_load() or {}
        self.paused = bool(data.get("paused"))
        self.actions = int(data.get("actions", 0))
        self.refused = int(data.get("refused", 0))
        self._recompute()
        self._listen()
        self._task = self.entry.async_create_background_task(self.hass, self._run(), f"lumashow {self.entry.entry_id} channel")

    async def async_stop(self, reason: str) -> None:
        """Ends the session (the Show is told why and drops everything at once) and stops for good."""
        self._stopping = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        for handle in (self._flush_handle, self._resnap_handle, self._camera_handle):
            if handle:
                handle.cancel()
        channel = self._channel
        if channel and not channel.closed:
            with contextlib.suppress(ChannelError, TimeoutError, ProtocolError):
                async with asyncio.timeout(2):
                    await channel.send(message("core", "bye", reason=reason))
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if channel:
            await channel.close()
        self.connected = False
        await self.store.async_save(self._stored())

    def _stored(self) -> dict[str, Any]:
        return {"paused": self.paused, "actions": self.actions, "refused": self.refused}

    def _save(self) -> None:
        self.store.async_delay_save(self._stored, 5)

    def _spawn(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        task = self.entry.async_create_task(self.hass, coroutine, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- the connection --------------------------------------------------------------------------------------------------------------

    def _pairing_key(self) -> bytes:
        return base64.b64decode(self.entry.data[CONF_KEY])

    def _refresh_key(self) -> bool:
        """Takes the key of the Show's ESPHome entry if it is another than the one held (the Show was paired again): whether it changed."""
        from .config_flow import esphome_key_for

        found = esphome_key_for(self.hass, self.mac)
        if found is None or found[1] == self.entry.data[CONF_KEY]:
            return False
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_KEY: found[1], CONF_ESPHOME_ENTRY: found[0]},
        )
        return True

    async def _run(self) -> None:
        backoff = BACKOFF_MIN
        while not self._stopping:
            try:
                channel = await self._connect()
                backoff = BACKOFF_MIN
                await self._session(channel)
            except asyncio.CancelledError:
                raise
            except AuthError:
                if not self._refresh_key():
                    self._start_reauth()
                    return
                continue
            except (ChannelError, ProtocolError, OSError, TimeoutError) as err:
                _LOGGER.debug("%s: the channel ended: %s", self.name, err)
            except Exception:
                _LOGGER.exception("%s: the channel failed", self.name)
            finally:
                self._lost()
            if self._stopping:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)

    async def _connect(self) -> Channel:
        return await Channel.connect(
            self.entry.data[CONF_HOST],
            self.entry.data[CONF_PORT],
            self._pairing_key(),
            timeout=CONNECT_TIMEOUT,
            expected_mac=self.mac,
        )

    def _start_reauth(self) -> None:
        if not self._reauth_started:
            self._reauth_started = True
            _LOGGER.warning("%s: the Show does not accept the key; pair it again", self.name)
            self.entry.async_start_reauth(self.hass)

    def _lost(self) -> None:
        """The session is over: what was agreed is not any more."""
        was = self.connected
        self.connected = False
        self.agreed = {}
        self._channel = None
        self._sent.clear()
        self._dirty.clear()
        self._structure = ""
        for handle in (self._flush_handle, self._camera_handle):
            if handle:
                handle.cancel()
        self._flush_handle = self._camera_handle = None
        for task in list(self._tasks):
            task.cancel()
        if was:
            for name, handler in module_handlers(self.hass).items():
                self._spawn(handler.async_session(self, False), f"lumashow module {name} down")
            self.signal()

    async def _session(self, channel: Channel) -> None:
        self._channel = channel
        handlers = module_handlers(self.hass)
        modules = dict(MODULES)
        modules.update({name: handler.version for name, handler in handlers.items() if getattr(handler, "offers", lambda _hub: True)(self)})
        await channel.send(
            message(
                "core",
                "hello",
                proto=[PROTO],
                client={
                    "name": DOMAIN,
                    "version": VERSION,
                    "ha_version": HA_VERSION,
                    "instance": await instance_id.async_get(self.hass),
                    "entry": self.entry.entry_id,
                },
                modules=modules,
            )
        )
        async with asyncio.timeout(CONNECT_TIMEOUT):
            welcome = await channel.receive()
        if welcome is None:
            raise ChannelError("the Show closed the connection before it said welcome")
        if welcome.get("m") != "core" or welcome.get("t") != "welcome":
            raise ChannelError(f"the Show answered hello with {welcome.get('m')}.{welcome.get('t')}: {welcome.get('message', '')}")
        if welcome.get("proto") != PROTO:
            raise ChannelError("the Show speaks another version of the protocol")
        self.agreed = {
            str(name): int(version) for name, version in (welcome.get("modules") or {}).items() if name in modules and isinstance(version, int)
        }
        self.device = welcome.get("device") if isinstance(welcome.get("device"), dict) else {}
        self._update_device()
        self.connected = True
        self._reauth_started = False
        self.signal()
        ping = self.entry.async_create_background_task(self.hass, self._pinger(channel), "lumashow ping")
        try:
            await self._push_everything()
            for name, handler in handlers.items():
                if name in self.agreed:
                    self._spawn(handler.async_session(self, True), f"lumashow module {name} up")
            while True:
                async with asyncio.timeout(SILENT_AFTER):
                    incoming = await channel.receive()
                if incoming is None:
                    return
                if await self._dispatch(incoming):
                    return
        finally:
            ping.cancel()
            await asyncio.gather(ping, return_exceptions=True)
            await channel.close()

    async def _pinger(self, channel: Channel) -> None:
        while True:
            await asyncio.sleep(PING_EVERY)
            self._ping += 1
            await channel.send(message("core", "ping", n=self._ping))

    def _update_device(self) -> None:
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, self.mac)},
            connections={(dr.CONNECTION_NETWORK_MAC, self.mac)},
            manufacturer=str(self.device.get("manufacturer") or "LumaShow"),
            model=str(self.device.get("model") or "") or None,
            name=self.name,
            sw_version=str(self.device.get("sw_version") or "") or None,
        )

    async def _send(self, outgoing: Message) -> bool:
        """Sends to the Show; ``False`` when there is no session (the read loop sees the end of one that broke)."""
        channel = self._channel
        if channel is None or not self.connected:
            return False
        try:
            await channel.send(outgoing)
        except (ChannelError, ProtocolError) as err:
            _LOGGER.debug("%s: could not send: %s", self.name, err)
            return False
        return True

    async def async_send_module(self, module: str, kind: str, fields: Mapping[str, Any]) -> bool:
        """Sends a message of a module a handler owns (``alarms``): only in a session that agreed it, and ``False`` when there is none."""
        if module in MODULES or module not in self.agreed:
            return False
        # Not `message(module, kind, **fields)`: a module's field may be named `kind` too (a camera event's).
        return await self._send({**fields, "m": module, "t": kind})

    def spawn_module_send(self, module: str, kind: str, fields: Mapping[str, Any]) -> None:
        """``async_send_module`` from a callback (a state change): the send is a task of the entry's."""
        if module in MODULES or module not in self.agreed:
            return
        self._spawn(self.async_send_module(module, kind, fields), f"lumashow module {module} {kind}")

    def shared_entities(self) -> Mapping[str, Shared]:
        """What is shared with this Show now, by entity id (a module reads it; the hub owns it)."""
        return self._allowed

    async def _dispatch(self, incoming: Message) -> bool:
        """Handles one message of the Show; ``True`` when the session is over."""
        module, kind = incoming["m"], incoming["t"]
        if module == "core":
            if kind == "ping":
                await self._send(message("core", "pong", n=incoming.get("n", 0)))
            elif kind == "bye":
                _LOGGER.debug("%s: the Show ended the session (%s)", self.name, incoming.get("reason"))
                return True
            elif kind == "error":
                _LOGGER.warning("%s: the Show reported an error: %s %s", self.name, incoming.get("code"), incoming.get("message"))
            return False
        if module not in self.agreed:
            return False
        if module == "states" and kind == "resync":
            await self._push_snapshot()
        elif module == "actions" and kind == "call":
            self._spawn(self._handle_call(incoming), "lumashow call")
        elif module == "cameras" and kind == "refresh":
            ids = incoming.get("ids")
            await self._push_cameras([i for i in ids if isinstance(i, str)] if isinstance(ids, list) else None)
        elif module == "notify" and kind == "dismissed":
            _LOGGER.debug("%s: %s was dismissed on the Show", self.name, incoming.get("id"))
        elif module in (handlers := module_handlers(self.hass)):
            body = {k: v for k, v in incoming.items() if k not in ("m", "t")}
            self._spawn(handlers[module].async_message(self, kind, body), f"lumashow module {module}")
        return False

    # -- what is shared --------------------------------------------------------------------------------------------------------------

    def _recompute(self) -> None:
        self._allowed = resolve(self.hass, self.options)

    def _verbs(self, entity_id: str) -> tuple[list[str], list[str]]:
        state = self.hass.states.get(entity_id)
        control, sensitive = control_flags(self.options, entity_id)
        return verb_lists(
            entity_id,
            state.attributes if state else None,
            control=control and not self.paused,
            sensitive=sensitive and not self.paused,
        )

    def _record(self, shared: Shared, areas: Mapping[str, str]) -> dict[str, Any]:
        state = self.hass.states.get(shared.entity_id)
        attrs = filter_attributes(shared.domain, state.attributes) if state else {}
        record: dict[str, Any] = {
            "id": shared.entity_id,
            "name": shared.name,
            "state": state.state if state else "unavailable",
            "attrs": attrs,
        }
        if shared.area_id in areas:
            record["area"] = shared.area_id
        if state:
            record["changed"] = state.last_changed.timestamp()
        if shared.minor:
            record["minor"] = True
        ordinary, needing_pin = self._verbs(shared.entity_id)
        if ordinary:
            record["ctl"] = ordinary
        if needing_pin:
            record["ctl_pin"] = needing_pin
        return record

    def _snapshot_messages(self) -> list[Message]:
        areas = area_names(self.hass, (s.area_id for s in self._allowed.values() if s.area_id))
        records = [self._record(shared, areas) for shared in self._allowed.values()]
        self._structure = self._structure_of(records, areas)
        self._sent = {r["id"]: _signature(r["state"], r["attrs"], r.get("changed")) for r in records}
        policy = {"paused": self.paused, "pin_set": bool(self.options.get(OPT_PIN_HASH))}
        area_list = [{"id": i, "name": n} for i, n in areas.items()]
        parts: list[list[dict[str, Any]]] = [[]]
        size = 0
        for record in records:
            length = len(json.dumps(record, separators=(",", ":")))
            if parts[-1] and size + length > MAX_PART - 2048:
                parts.append([])
                size = 0
            parts[-1].append(record)
            size += length
        return [
            message(
                "states",
                "snapshot",
                rev=self.rev,
                part=index,
                more=index < len(parts) - 1,
                policy=policy,
                areas=area_list if index == 0 else [],
                entities=part,
            )
            for index, part in enumerate(parts)
        ]

    @staticmethod
    def _structure_of(records: list[dict[str, Any]], areas: Mapping[str, str]) -> str:
        return json.dumps(
            [
                [(r["id"], r["name"], r.get("area"), r.get("minor"), r.get("ctl"), r.get("ctl_pin")) for r in records],
                sorted(areas.items()),
            ],
            sort_keys=True,
        )

    async def _push_everything(self) -> None:
        if "states" in self.agreed:
            await self._push_snapshot()
        if "cameras" in self.agreed:
            await self._push_cameras(None)
        if "notify" in self.agreed:
            await self._push_notifications()

    async def _push_snapshot(self) -> None:
        self._recompute()
        self._listen_states()
        self.rev += 1
        for part in self._snapshot_messages():
            if not await self._send(part):
                return
        self._mark_push()
        # A module that follows the shared set (camera events) is told that it may have changed.
        for name, handler in module_handlers(self.hass).items():
            changed = getattr(handler, "async_shared_changed", None)
            if name in self.agreed and changed is not None:
                self._spawn(changed(self), f"lumashow module {name} shared")

    def _mark_push(self) -> None:
        now = self.clock()
        self.last_push = datetime.now(UTC)
        if now - self._last_push_signal >= LAST_PUSH_EVERY:
            self._last_push_signal = now
            self.signal()

    # -- following the states ---------------------------------------------------------------------------------------------------------

    def _listen(self) -> None:
        bus = self.hass.bus
        self._unsubs.append(bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._entity_registry_event))
        self._unsubs.append(bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._registry_event))
        self._unsubs.append(bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, self._registry_event))
        self._listen_states()
        if "persistent_notification" in self.hass.config.components:
            from homeassistant.components import persistent_notification

            self._unsubs.append(persistent_notification.async_register_callback(self.hass, self._persistent_event))

    def _listen_states(self) -> None:
        if self._state_unsub:
            self._state_unsub()
            self._state_unsub = None
        if self._allowed:
            self._state_unsub = async_track_state_change_event(self.hass, list(self._allowed), self._state_event)

    @callback
    def _state_event(self, event: Event) -> None:
        self._dirty.add(event.data["entity_id"])
        if self._flush_handle is None and self.connected and not self._stopping:
            self._flush_handle = self.hass.loop.call_later(PUSH_DEBOUNCE, self._flush_due)

    @callback
    def _flush_due(self) -> None:
        self._flush_handle = None
        if self.connected:
            self._spawn(self._flush(), "lumashow update")

    async def _flush(self) -> None:
        """What changed since the last push, as updates (an entity whose pushed values are the same is not sent)."""
        dirty, self._dirty = self._dirty, set()
        records: list[dict[str, Any]] = []
        for entity_id in sorted(dirty):
            shared = self._allowed.get(entity_id)
            if shared is None:
                continue
            state = self.hass.states.get(entity_id)
            value = state.state if state else "unavailable"
            attrs = filter_attributes(shared.domain, state.attributes) if state else {}
            changed = state.last_changed.timestamp() if state else None
            signature = _signature(value, attrs, changed)
            if self._sent.get(entity_id) == signature:
                continue
            self._sent[entity_id] = signature
            record: dict[str, Any] = {"id": entity_id, "state": value, "attrs": attrs}
            if changed is not None:
                record["changed"] = changed
            records.append(record)
        if not records:
            return
        batches: list[list[dict[str, Any]]] = [[]]
        size = 0
        for record in records:
            length = len(json.dumps(record, separators=(",", ":")))
            if batches[-1] and size + length > MAX_PART - 2048:
                batches.append([])
                size = 0
            batches[-1].append(record)
            size += length
        for batch in batches:
            if not await self._send(message("states", "update", rev=self.rev, entities=batch)):
                return
        self._mark_push()

    @callback
    def _entity_registry_event(self, event: Event) -> None:
        data = event.data
        entity_id = data.get("entity_id", "")
        old = data.get("old_entity_id")
        if data.get("action") == "update" and old and old != entity_id:
            self._renamed(old, entity_id)
            return
        if entity_id in self._allowed or (data.get("action") == "remove" and entity_id in listed(self.options)):
            self._schedule_resnap()

    @callback
    def _registry_event(self, event: Event) -> None:
        self._schedule_resnap()

    @callback
    def _renamed(self, old: str, new: str) -> None:
        """An entity of the list got a new id: the list follows it."""
        if old not in listed(self.options):
            return
        entities = [new if e == old else e for e in listed(self.options)]
        self.hass.config_entries.async_update_entry(self.entry, options={**self.options, OPT_ENTITIES: list(dict.fromkeys(entities))})
        self._schedule_resnap()

    @callback
    def _schedule_resnap(self) -> None:
        if self._resnap_handle is None and self.connected and not self._stopping:
            self._resnap_handle = self.hass.loop.call_later(1.0, self._resnap_due)

    @callback
    def _resnap_due(self) -> None:
        self._resnap_handle = None
        self._spawn(self._resnap(), "lumashow resnapshot")

    async def _resnap(self) -> None:
        """Names, areas or the set changed: a new snapshot, if what the Show is told of the set really differs."""
        if not self.connected or "states" not in self.agreed:
            return
        before = self._structure
        self._recompute()
        areas = area_names(self.hass, (s.area_id for s in self._allowed.values() if s.area_id))
        records = [self._record(shared, areas) for shared in self._allowed.values()]
        if self._structure_of(records, areas) == before:
            return
        await self._push_snapshot()
        if "cameras" in self.agreed:
            await self._push_cameras(None)

    # -- control -----------------------------------------------------------------------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        """The *Pause control* switch: with it on, every call is refused and the Show is told it has no verbs."""
        if paused == self.paused:
            return
        self.paused = paused
        self._save()
        self.signal()
        if self.connected:
            self._spawn(self._push_snapshot(), "lumashow policy")

    def _rate_ok(self) -> bool:
        now = self.clock()
        while self._rate and now - self._rate[0] >= RATE_WINDOW:
            self._rate.popleft()
        if len(self._rate) >= RATE_CALLS:
            return False
        self._rate.append(now)
        return True

    def _decide(self, call: Mapping[str, Any]) -> tuple[str, str, dict[str, Any], list[Shared], str, bool]:
        """Checks a call; returns ``(domain, verb, args, entities, origin, needs_pin)`` or raises ``Refusal``."""
        if self.paused:
            raise Refusal("paused")
        if not self._rate_ok():
            raise Refusal("rate_limited")
        ids = call.get("entity_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise Refusal("bad_args", "entity_ids is not a list of entity ids")
        if len(ids) > MAX_CALL_ENTITIES:
            raise Refusal("too_many")
        verb = call.get("verb")
        domains = {domain_of(i) for i in ids}
        if len(domains) != 1:
            raise Refusal("bad_args", "a call names entities of one domain")
        (domain,) = domains
        if not isinstance(verb, str) or verb not in verbs_of(domain):
            raise Refusal("unknown_verb", f"{domain}.{verb}")
        try:
            args = validate_args(domain, verb, call.get("args"))
        except BadArgs as err:
            raise Refusal("bad_args", str(err)) from err
        origin = call.get("origin")
        origin = origin if origin in ORIGINS else "plugin"
        shared_list: list[Shared] = []
        needs_pin = False
        for entity_id in dict.fromkeys(ids):
            shared = self._allowed.get(entity_id)
            if shared is None:
                raise Refusal("not_allowed", f"{entity_id} is not shared with this Show")
            ordinary, needing_pin = self._verbs(entity_id)
            state = self.hass.states.get(entity_id)
            if verb in ordinary:
                pass
            elif verb in needing_pin:
                needs_pin = True
            elif is_sensitive(domain, verb, state.attributes if state else None):
                raise Refusal("sensitive_disabled", f"{domain}.{verb} on {entity_id}")
            else:
                raise Refusal("not_allowed", f"{domain}.{verb} on {entity_id}: the Show may not act on it")
            shared_list.append(shared)
        if needs_pin:
            self._precheck_pin(call, origin)
        return domain, verb, args, shared_list, origin, needs_pin

    def _precheck_pin(self, call: Mapping[str, Any], origin: str) -> None:
        if self.clock() < self._locked_until:
            raise Refusal("locked_out")
        if origin != "touch":
            raise Refusal("needs_touch", f"origin {origin}")
        if call.get("confirmed") is not True:
            raise Refusal("needs_confirm")
        pin = call.get("pin")
        if not isinstance(pin, str) or not pin:
            raise Refusal("pin_required")

    async def _check_pin(self, pin: str) -> None:
        """The PIN against the one that was set; a wrong one is counted, and too many lock the sensitive verbs for a while."""
        salt, expected = self.options.get(OPT_PIN_SALT), self.options.get(OPT_PIN_HASH)
        if not salt or not expected:
            raise Refusal("pin_required", "no PIN is set")
        good = await self.hass.async_add_executor_job(pin_ok, pin, salt, expected)
        if good:
            self._pin_failures.clear()
            return
        now = self.clock()
        self._pin_failures.append(now)
        while self._pin_failures and now - self._pin_failures[0] > PIN_WINDOW:
            self._pin_failures.popleft()
        if len(self._pin_failures) >= PIN_FAILURES:
            self._locked_until = now + PIN_LOCKOUT
            self._pin_failures.clear()
            _LOGGER.warning("%s: too many wrong PINs: sensitive actions are locked for %d minutes", self.name, PIN_LOCKOUT // 60)
            raise Refusal("locked_out")
        raise Refusal("pin_wrong")

    async def _handle_call(self, call: Mapping[str, Any]) -> None:
        """One ``actions.call``: decided here, done here, answered once, and written down."""
        op = call.get("op", 0)
        context = Context()
        try:
            domain, verb, args, entities, origin, needs_pin = self._decide(call)
            if needs_pin:
                await self._check_pin(str(call.get("pin")))
        except Refusal as refusal:
            await self._refused(call, refusal, context)
            await self._send(
                message(
                    "actions",
                    "result",
                    op=op,
                    ok=False,
                    code=refusal.code,
                    message=refusal.detail or refusal.code,
                )
            )
            return
        results = await asyncio.gather(*(self._do_one(domain, verb, args, shared, origin, context) for shared in entities))
        ok = all(r["ok"] for r in results)
        reply = message("actions", "result", op=op, ok=ok, results=results)
        if not ok:
            reply["code"] = next(r["code"] for r in results if not r["ok"])
        await self._send(reply)

    async def _do_one(
        self,
        domain: str,
        verb: str,
        args: dict[str, Any],
        shared: Shared,
        origin: str,
        context: Context,
    ) -> dict[str, Any]:
        code: str | None = None
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                await self.hass.services.async_call(
                    domain,
                    verb,
                    {"entity_id": shared.entity_id, **args},
                    blocking=True,
                    context=context,
                )
        except TimeoutError:
            code = "timeout"
        except (ServiceNotFound, HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning("%s: %s.%s on %s failed: %s", self.name, domain, verb, shared.entity_id, err)
            code = "failed"
        self.actions += 1
        text = describe(verb, shared.name, args)
        self.last_action = f"{text}" if code is None else f"{text} (failed: {code})"
        self._save()
        await self._audit(shared.entity_id, text if code is None else f"tried to {text}: {code}", context)
        self.hass.bus.async_fire(
            EVENT_ACTION,
            {
                "device": self.name,
                "entity_id": shared.entity_id,
                "verb": f"{domain}.{verb}",
                "origin": origin,
                "ok": code is None,
                "code": code,
            },
            context=context,
        )
        self.signal()
        result: dict[str, Any] = {"entity_id": shared.entity_id, "ok": code is None}
        if code:
            result["code"] = code
        return result

    async def _refused(self, call: Mapping[str, Any], refusal: Refusal, context: Context) -> None:
        """A call that was not carried out: counted, logged, and (not more than every ten seconds) in the logbook."""
        self.refused += 1
        self.last_action = f"refused: {refusal.code}"
        self._save()
        ids = call.get("entity_ids")
        first = ids[0] if isinstance(ids, list) and ids and isinstance(ids[0], str) else None
        _LOGGER.warning(
            "%s: a call was refused (%s%s): %s %s",
            self.name,
            refusal.code,
            f", {refusal.detail}" if refusal.detail else "",
            call.get("verb"),
            ids,
        )
        self.hass.bus.async_fire(
            EVENT_ACTION,
            {
                "device": self.name,
                "entity_id": first,
                "verb": call.get("verb") if isinstance(call.get("verb"), str) else None,
                "origin": call.get("origin") if call.get("origin") in ORIGINS else "plugin",
                "ok": False,
                "code": refusal.code,
            },
            context=context,
        )
        now = self.clock()
        if now - self._last_refusal_log >= REFUSAL_LOG_EVERY:
            self._last_refusal_log = now
            await self._audit(first, f"was refused ({refusal.code}): {call.get('verb')}", context)
        self.signal()

    async def _audit(self, entity_id: str | None, text: str, context: Context) -> None:
        """Writes ``<Show>: <text>`` in the logbook, against the entity."""
        if not self.hass.services.has_service("logbook", "log"):
            return
        # The service renders its message as a template: nothing a Show sent is allowed to be one.
        safe = text.replace("{", "(").replace("}", ")")
        data: dict[str, Any] = {"name": f"{self.name}:", "message": safe, "domain": DOMAIN}
        if entity_id:
            data["entity_id"] = entity_id
        with contextlib.suppress(HomeAssistantError, vol.Invalid):
            await self.hass.services.async_call("logbook", "log", data, blocking=True, context=context)

    # -- cameras ------------------------------------------------------------------------------------------------------------------------

    async def _push_cameras(self, ids: list[str] | None) -> None:
        """The shared cameras with signed, expiring addresses of their pictures (and their streams, if the person allows it)."""
        if "cameras" not in self.agreed:
            return
        cameras = [s for s in self._allowed.values() if s.domain == "camera" and (ids is None or s.entity_id in ids)]
        try:
            base = get_url(self.hass, allow_external=False, prefer_external=False, allow_cloud=False)
        except NoURLAvailableError:
            base = None
        records = [await self._camera_record(shared, base) for shared in cameras]
        await self._send(message("cameras", "list", rev=self.rev, partial=ids is not None, cameras=records))
        if self._camera_handle:
            self._camera_handle.cancel()
            self._camera_handle = None
        if any(s.domain == "camera" for s in self._allowed.values()) and not self._stopping:
            self._camera_handle = self.hass.loop.call_later(SNAPSHOT_URL_REFRESH, self._cameras_due)

    @callback
    def _cameras_due(self) -> None:
        self._camera_handle = None
        if self.connected:
            self._spawn(self._push_cameras(None), "lumashow cameras")

    async def _camera_record(self, shared: Shared, base: str | None) -> dict[str, Any]:
        record: dict[str, Any] = {"id": shared.entity_id, "name": shared.name}
        if shared.area_id:
            record["area"] = shared.area_id
        if base:
            from homeassistant.components.http.auth import async_sign_path

            path = async_sign_path(
                self.hass,
                f"/api/camera_proxy/{shared.entity_id}",
                timedelta(seconds=SNAPSHOT_URL_LIFETIME),
            )
            record["snapshot"] = {"url": base + path, "expires": int(time.time()) + SNAPSHOT_URL_LIFETIME}
        if self.options.get(OPT_SHARE_STREAMS):
            stream = await self._stream_of(shared.entity_id)
            if stream:
                record["streams"] = [stream]
        return record

    async def _stream_of(self, entity_id: str) -> dict[str, str] | None:
        from homeassistant.components.camera import async_get_stream_source

        try:
            url = await async_get_stream_source(self.hass, entity_id)
        except (HomeAssistantError, KeyError):
            return None
        if not url:
            return None
        scheme = url.split(":", 1)[0].lower()
        if scheme in ("rtsp", "rtsps"):
            return {"kind": "rtsp", "url": url}
        if ".m3u8" in url:
            return {"kind": "hls", "url": url}
        if scheme in ("http", "https"):
            return {"kind": "mjpeg", "url": url}
        return None

    # -- notifications ------------------------------------------------------------------------------------------------------------------

    async def async_notify(self, title: str | None, text: str) -> None:
        """A message for the Show's screen (the notify entity)."""
        if not self.connected or "notify" not in self.agreed:
            raise HomeAssistantError(f"{self.name} is not connected")
        await self._send(
            message(
                "notify",
                "post",
                id=f"service-{uuid.uuid4().hex[:12]}",
                title=(title or self.name)[:200],
                message=text[:1000],
                level="info",
                source="service",
                ttl_s=3600,
            )
        )

    async def _push_notifications(self) -> None:
        items: list[dict[str, Any]] = []
        if self.options.get(OPT_FORWARD_NOTIFICATIONS) and "persistent_notification" in self.hass.config.components:
            items = [self._persistent_item(n) for n in _standing_notifications(self.hass).values()][:32]
        await self._send(message("notify", "snapshot", items=items))

    @staticmethod
    def _persistent_item(notification: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(notification["notification_id"])[:128],
            "title": str(notification.get("title") or "")[:200],
            "message": str(notification.get("message") or "")[:1000],
            "level": "info",
            "source": "persistent_notification",
        }

    @callback
    def _persistent_event(self, update_type: Any, notifications: Mapping[str, Mapping[str, Any]]) -> None:
        if not (self.connected and "notify" in self.agreed and self.options.get(OPT_FORWARD_NOTIFICATIONS)):
            return
        for notification in notifications.values():
            item = self._persistent_item(notification)
            if str(update_type) == "removed":
                self._spawn(self._send(message("notify", "clear", id=item["id"])), "lumashow notify clear")
            else:
                self._spawn(self._send(message("notify", "post", **item)), "lumashow notify post")
