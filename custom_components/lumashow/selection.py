"""What is shared with a Show: the list the person built, and the quick starts that add to it.

**Nothing is shared until the person adds something.** The options hold one list of entity ids (``entities``): exactly what the Show is given.
The quick starts (by area, by device, by type) only *add* to that list, once, when the person asks; an area that gains a device later does not
share it. Per-entity editing, and the optional "copy what Assist may see", work on the same list. Entities that are gone are not sent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    MAX_ENTITIES,
    OPT_CONTROL_DOMAINS,
    OPT_ENTITIES,
    OPT_PIN_HASH,
    OPT_READ_ONLY,
    OPT_SENSITIVE,
    OPT_UNLOCK_CONFIRM_ONLY,
)
from .policy import NAMED_ONLY_DOMAINS, domain_of, verb_lists, verbs_of

# The kinds a person picks by, and the domains each stands for.
TYPE_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    "lights": ("light",),
    "switches": ("switch", "input_boolean"),
    "climate": ("climate", "humidifier", "water_heater"),
    "covers": ("cover",),
    "locks": ("lock",),
    "alarm": ("alarm_control_panel",),
    "cameras": ("camera",),
    "media": ("media_player",),
    "fans": ("fan",),
    "scenes": ("scene",),
    "sensors": ("sensor",),
    "binary_sensors": ("binary_sensor",),
    "events": ("event",),
    "buttons": ("button", "input_button"),
    "controls": ("number", "input_number", "select", "input_select"),
    "vacuums": ("vacuum",),
    "valves": ("valve",),
    "sirens": ("siren",),
    "people": ("person",),
    "scripts": ("script",),
    "automations": ("automation",),
}
# What a quick start by area or device offers to begin with: every kind a person does not have to name.
DEFAULT_GROUPS: Final = tuple(name for name, domains in TYPE_GROUPS.items() if not any(domain in NAMED_ONLY_DOMAINS for domain in domains))


@dataclass(frozen=True)
class Shared:
    """One entity that is shared, as the Show is told of it."""

    entity_id: str
    name: str
    area_id: str | None
    minor: bool

    @property
    def domain(self) -> str:
        return domain_of(self.entity_id)


def domains_of(groups: Iterable[str]) -> set[str]:
    """The domains the kinds ``groups`` stand for."""
    return {domain for group in groups for domain in TYPE_GROUPS.get(group, ())}


def _area_of(entry: er.RegistryEntry, devices: dr.DeviceRegistry) -> str | None:
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := devices.async_get(entry.device_id)):
        return device.area_id
    return None


def _is_minor(entry: er.RegistryEntry) -> bool:
    return entry.entity_category is not None or entry.hidden_by is not None


def _chosen(
    entries: Iterable[er.RegistryEntry],
    domains: set[str] | None,
    include_minor: bool,
    *,
    bulk: bool,
) -> list[str]:
    """The ids of ``entries`` that a quick start adds, sorted."""
    out = []
    for entry in entries:
        if entry.disabled_by is not None:
            continue
        domain = entry.domain
        if bulk and domain in NAMED_ONLY_DOMAINS:
            continue
        if domains is not None and domain not in domains:
            continue
        if not include_minor and _is_minor(entry):
            continue
        out.append(entry.entity_id)
    return sorted(out)


def pick_by_area(
    hass: HomeAssistant,
    area_ids: Iterable[str],
    groups: Iterable[str] | None = None,
    include_minor: bool = False,
) -> list[str]:
    """The entities standing in these areas (their own, or their device's), of these kinds."""
    wanted = set(area_ids)
    devices = dr.async_get(hass)
    entries = [entry for entry in er.async_get(hass).entities.values() if _area_of(entry, devices) in wanted]
    domains = None if groups is None else domains_of(groups)
    return _chosen(entries, domains, include_minor, bulk=True)


def pick_by_device(
    hass: HomeAssistant,
    device_ids: Iterable[str],
    groups: Iterable[str] | None = None,
    include_minor: bool = False,
) -> list[str]:
    """The entities of these devices, of these kinds."""
    registry = er.async_get(hass)
    entries: list[er.RegistryEntry] = []
    for device_id in device_ids:
        entries.extend(er.async_entries_for_device(registry, device_id))
    domains = None if groups is None else domains_of(groups)
    return _chosen(entries, domains, include_minor, bulk=True)


def pick_by_type(
    hass: HomeAssistant,
    groups: Iterable[str],
    area_ids: Iterable[str] | None = None,
    include_minor: bool = False,
) -> list[str]:
    """Every entity of these kinds in the house (or only in these areas): naming a kind is naming it, scripts and automations too."""
    domains = domains_of(groups)
    devices = dr.async_get(hass)
    wanted = None if area_ids is None else set(area_ids)
    entries = [entry for entry in er.async_get(hass).entities.values() if wanted is None or _area_of(entry, devices) in wanted]
    return _chosen(entries, domains, include_minor, bulk=False)


def assist_exposed(hass: HomeAssistant) -> list[str]:
    """What Assist may see (an optional helper to start from: it is a voice assistant's list, and usually almost everything)."""
    from homeassistant.components.homeassistant.exposed_entities import (
        async_should_expose,
    )

    return sorted(
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.disabled_by is None and async_should_expose(hass, "conversation", entry.entity_id)
    )


def merged(current: Iterable[str], added: Iterable[str]) -> list[str]:
    """``current`` followed by what ``added`` has that it does not, with no repeats."""
    out = list(dict.fromkeys(current))
    seen = set(out)
    for entity_id in added:
        if entity_id not in seen:
            seen.add(entity_id)
            out.append(entity_id)
    return out


def listed(options: Mapping[str, Any]) -> list[str]:
    """The list of entity ids in the options, with no repeats and no more than a Show takes."""
    raw = options.get(OPT_ENTITIES) or []
    return list(dict.fromkeys(str(e) for e in raw if isinstance(e, str)))[:MAX_ENTITIES]


def resolve(hass: HomeAssistant, options: Mapping[str, Any]) -> dict[str, Shared]:
    """The shared entities that exist now, in the order of the list."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    out: dict[str, Shared] = {}
    for entity_id in listed(options):
        entry = entities.async_get(entity_id)
        state = hass.states.get(entity_id)
        if entry is None and state is None:
            continue
        if entry is not None and entry.disabled_by is not None:
            continue
        name = (state.name if state else None) or (entry.name or entry.original_name if entry else None) or entity_id
        out[entity_id] = Shared(
            entity_id=entity_id,
            name=str(name),
            area_id=_area_of(entry, devices) if entry else None,
            minor=_is_minor(entry) if entry else False,
        )
    return out


def area_names(hass: HomeAssistant, area_ids: Iterable[str]) -> dict[str, str]:
    """The names of these areas (only these: a name beyond the shared entities' areas is never sent)."""
    registry = ar.async_get(hass)
    out: dict[str, str] = {}
    for area_id in dict.fromkeys(a for a in area_ids if a):
        if area := registry.async_get_area(area_id):
            out[area_id] = area.name
    return out


def control_flags(options: Mapping[str, Any], entity_id: str) -> tuple[bool, bool]:
    """``(control, sensitive)`` for an entity by the options: whether its ordinary verbs, and its sensitive ones, are enabled.

    A sensitive verb needs a PIN to exist, so the second flag is false without one.
    """
    domain = domain_of(entity_id)
    control = domain in (options.get(OPT_CONTROL_DOMAINS) or ()) and entity_id not in (options.get(OPT_READ_ONLY) or ()) and bool(verbs_of(domain))
    sensitive = bool(options.get(OPT_PIN_HASH)) and entity_id in (options.get(OPT_SENSITIVE) or ())
    return control, sensitive


def confirm_unlock_flag(options: Mapping[str, Any], entity_id: str) -> bool:
    """Whether a lock is unlocked by a confirmed touch alone: the device's option is on and the person allowed this lock's sensitive verbs.

    It does not need a PIN to exist. It never applies to another domain.
    """
    return bool(options.get(OPT_UNLOCK_CONFIRM_ONLY)) and domain_of(entity_id) == "lock" and entity_id in (options.get(OPT_SENSITIVE) or ())


def summary(hass: HomeAssistant, options: Mapping[str, Any]) -> tuple[int, int]:
    """``(shared, controllable)``: how many entities the Show is given and how many of them it may act on."""
    shared = resolve(hass, options)
    controllable = 0
    for entity_id in shared:
        state = hass.states.get(entity_id)
        control, sensitive = control_flags(options, entity_id)
        ordinary, needing_pin = verb_lists(
            entity_id,
            state.attributes if state else None,
            control=control,
            sensitive=sensitive,
            confirm_unlock=confirm_unlock_flag(options, entity_id),
        )
        if ordinary or needing_pin:
            controllable += 1
    return len(shared), controllable
