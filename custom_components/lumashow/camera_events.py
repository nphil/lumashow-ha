"""Module ``camera_events``: what a shared camera's detectors see, as it happens, for a device that shows a pop-up or a card.

A camera in Home Assistant comes with detectors on the same device: Reolink's *Person*, *Vehicle*, *Pet*, *Visitor* binary sensors,
Scrypted's *Object detected* and *Doorbell* event entities, a plain *Motion*. A device that shows the camera (a LumaTV over whatever is
playing, a Show's card) wants one word for each of them: **a person at the front door**, now. That is what this module sends: one message
per detection, naming the camera the person already shared and the kind of thing seen, nothing of any entity the person did not share.

- **Which detectors count.** The ``binary_sensor`` and ``event`` entities of the camera entity's own device whose name says what they detect
  (``classify``), plus ``event`` entities elsewhere whose ``camera`` attribute is the camera's name (an animal watcher that names the camera it
  looked through). Hidden and disabled entities never count. A detector that cannot be read as one of the kinds is not a source.
- **Kinds.** ``person``, ``vehicle``, ``animal``, ``package``, ``doorbell``, ``motion``. An event entity that announces many classes (``event_type``
  ``person``, ``vehicle``, ...) is read at each event.
- **One event, once.** A binary sensor fires when it turns on; an event entity when it has a new moment. Two of one camera and one kind within
  ``HOLD`` seconds are one event (a detector that re-fires on every frame; two detectors that see the same person).
- **Offered only** when the person turned on *Camera events* for that device (an option of its entry; off by default like the others), and then
  for the cameras of its shared set only. With no device agreeing it nothing here runs: no subscription, no timer.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import OPT_CAMERA_EVENTS

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

MODULE: Final = "camera_events"
VERSION: Final = 1
KINDS: Final = ("person", "vehicle", "animal", "package", "doorbell", "motion")
# Two events of one camera and one kind closer than this are one event.
HOLD: Final = 2.0
# The longest label (what the detector called it: a species, a class) that is sent.
MAX_LABEL: Final = 64

# The words a detector's name or class is read by, in the order they decide: what is seen before where ("person" before "doorbell",
# because a doorbell camera's person sensor has both words in its entity id), and "motion" last (a "vehicle motion" sensor is a vehicle's).
_WORDS: Final[tuple[tuple[str, frozenset[str]], ...]] = (
    ("person", frozenset({"person", "people", "human", "face", "pedestrian"})),
    ("vehicle", frozenset({"vehicle", "car", "truck", "bicycle", "bike", "motorcycle", "bus"})),
    ("animal", frozenset({"animal", "pet", "dog", "cat", "bird", "mammal", "wildlife", "dog_cat", "raccoon", "deer", "squirrel"})),
    ("package", frozenset({"package", "parcel", "delivery"})),
    ("doorbell", frozenset({"doorbell", "visitor", "ring", "rang", "chime"})),
    ("motion", frozenset({"motion", "movement", "moving"})),
)


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    word = []
    for ch in text.lower():
        if ch.isalnum():
            word.append(ch)
        else:
            if word:
                out.add("".join(word))
            word = []
    if word:
        out.add("".join(word))
    # "dog_cat" and "dog-cat" both read as the pair too.
    if {"dog", "cat"} <= out:
        out.add("dog_cat")
    return out


def kind_of_words(text: str) -> str | None:
    """The kind the words of ``text`` (a name, an entity id, an event class) say, or ``None``."""
    tokens = _tokens(text)
    for kind, words in _WORDS:
        if tokens & words:
            return kind
    return None


def classify(entity_id: str, name: str | None, device_class: str | None, event_types: Iterable[str] | None) -> str | None:
    """What a detector entity announces: a kind, ``"*"`` for an event entity whose class is read at each event, or ``None`` (not a detector).

    The entity's own name ("Person", "Visitor") is read first: an entity id carries the device's name too, and a doorbell camera's
    sensors all have "doorbell" in theirs.
    """
    if device_class == "doorbell":
        return "doorbell"
    object_id = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
    by_name = kind_of_words(name or "") or kind_of_words(object_id)
    if event_types:
        classes = {kind_of_words(t) for t in event_types} - {None}
        if len(classes) > 1:
            return "*"
        if classes:
            return by_name if by_name and by_name != "motion" else classes.pop()
    if by_name:
        return by_name
    if device_class in ("motion", "occupancy"):
        return "motion"
    return None


def _label(attrs: Mapping[str, Any]) -> str | None:
    for key in ("species", "label", "description"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text[:MAX_LABEL]
    return None


class CameraEventsModule:
    """The owner of module ``camera_events`` (registered in ``hass.data`` for the hubs to offer and to call)."""

    version = VERSION

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._watches: dict[str, _Watch] = {}

    @staticmethod
    def sharing(hub: Hub) -> bool:
        """Whether the person turned on *Camera events* for this device."""
        return bool(hub.options.get(OPT_CAMERA_EVENTS))

    def offers(self, hub: Hub) -> bool:
        return self.sharing(hub)

    async def async_session(self, hub: Hub, up: bool) -> None:
        key = hub.entry.entry_id
        if not up:
            watch = self._watches.pop(key, None)
            if watch:
                watch.stop()
            return
        if not self.sharing(hub):
            _LOGGER.debug("%s: camera events are not shared with this device", hub.name)
            return
        watch = self._watches.get(key)
        if watch is None:
            watch = self._watches[key] = _Watch(self.hass, hub)
        watch.rebuild()
        await watch.push_list()

    async def async_shared_changed(self, hub: Hub) -> None:
        """The shared set changed (a new snapshot went out): the detectors follow the cameras in it."""
        watch = self._watches.get(hub.entry.entry_id)
        if watch and watch.rebuild():
            await watch.push_list()

    async def async_message(self, hub: Hub, kind: str, body: Mapping[str, Any]) -> None:
        watch = self._watches.get(hub.entry.entry_id)
        if watch is None:
            return
        if kind == "refresh":
            await watch.push_list()
        else:
            _LOGGER.debug("%s: a message camera_events.%s is not one this version knows", hub.name, kind)

    def sources_of(self, hub: Hub) -> dict[str, list[str]]:
        """For tests and diagnostics: the detector entities watched for each shared camera."""
        watch = self._watches.get(hub.entry.entry_id)
        if watch is None:
            return {}
        out: dict[str, list[str]] = {}
        for source, targets in watch.sources.items():
            for camera, _kind in targets:
                out.setdefault(camera, []).append(source)
        return {camera: sorted(sources) for camera, sources in out.items()}


class _Watch:
    """One device's detectors: which entities are watched, for which camera, and the hold between events."""

    def __init__(self, hass: HomeAssistant, hub: Hub) -> None:
        self.hass = hass
        self.hub = hub
        # source entity -> [(camera entity, kind or "*")]
        self.sources: dict[str, list[tuple[str, str]]] = {}
        self._unsub: Any = None
        self._last: dict[tuple[str, str], float] = {}
        self.clock = time.monotonic

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        self.sources = {}
        self._last.clear()

    def rebuild(self) -> bool:
        """Finds the detectors of the shared cameras again; ``True`` when the set of them changed."""
        before = self.sources
        self.sources = self._find()
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self.sources:
            self._unsub = async_track_state_change_event(self.hass, list(self.sources), self._changed)
        return self.sources != before

    def _find(self) -> dict[str, list[tuple[str, str]]]:
        registry = er.async_get(self.hass)
        found: dict[str, list[tuple[str, str]]] = {}
        cameras = [s for s in self.hub.shared_entities().values() if s.domain == "camera"]
        for shared in cameras:
            own = registry.async_get(shared.entity_id)
            device_id = own.device_id if own else None
            if device_id:
                for entry in er.async_entries_for_device(registry, device_id):
                    if entry.domain not in ("binary_sensor", "event") or entry.hidden_by or entry.disabled_by:
                        continue
                    state = self.hass.states.get(entry.entity_id)
                    kind = classify(
                        entry.entity_id,
                        entry.name or entry.original_name,
                        entry.device_class or entry.original_device_class,
                        state.attributes.get("event_types") if state else None,
                    )
                    if kind:
                        found.setdefault(entry.entity_id, []).append((shared.entity_id, kind))
            # Watchers elsewhere that name the camera they looked through (an animal watcher's event entity: `camera: "Front Door Camera"`).
            for state in self.hass.states.async_all("event"):
                if state.attributes.get("camera") != shared.name:
                    continue
                entry = registry.async_get(state.entity_id)
                if entry and (entry.hidden_by or entry.disabled_by):
                    continue
                if any(camera == shared.entity_id for camera, _ in found.get(state.entity_id, [])):
                    continue
                kind = classify(state.entity_id, state.name, state.attributes.get("device_class"), state.attributes.get("event_types")) or "*"
                found.setdefault(state.entity_id, []).append((shared.entity_id, kind))
        return found

    def kinds(self) -> dict[str, set[str]]:
        """The kinds each shared camera can announce (``*`` counts for every kind an event entity lists)."""
        out: dict[str, set[str]] = {}
        for source, targets in self.sources.items():
            state = self.hass.states.get(source)
            for camera, kind in targets:
                kinds = out.setdefault(camera, set())
                if kind != "*":
                    kinds.add(kind)
                    continue
                types = state.attributes.get("event_types") if state else None
                for word in types or ():
                    found = kind_of_words(str(word))
                    if found:
                        kinds.add(found)
        for shared in self.hub.shared_entities().values():
            if shared.domain == "camera":
                out.setdefault(shared.entity_id, set())
        return out

    async def push_list(self) -> None:
        cameras = [{"id": camera, "kinds": [k for k in KINDS if k in kinds]} for camera, kinds in sorted(self.kinds().items())]
        await self.hub.async_send_module(MODULE, "list", {"cameras": cameras})

    @callback
    def _changed(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if new is None or new.state in ("unknown", "unavailable"):
            return
        if entity_id.startswith("binary_sensor."):
            if new.state != "on" or (old is not None and old.state == "on"):
                return
        elif old is not None and old.state == new.state:
            return
        for camera, kind in self.sources.get(entity_id, ()):
            if kind == "*":
                kind = kind_of_words(str(new.attributes.get("event_type", ""))) or "motion"
            now = self.clock()
            last = self._last.get((camera, kind))
            if last is not None and now - last < HOLD:
                continue
            self._last[(camera, kind)] = now
            fields: dict[str, Any] = {"camera": camera, "kind": kind, "at": new.last_changed.timestamp(), "source": entity_id}
            label = _label(new.attributes)
            if label:
                fields["label"] = label
            self.hub.spawn_module_send(MODULE, "event", fields)
