"""Adding a Show, and choosing what it is given.

**Adding.** The Show announces itself (``_lumashow._tcp``). Its ESPHome device, if Home Assistant has one, holds the key that pairs the two: this
flow finds that entry by the Show's hardware address and uses its key, so nothing is typed. Only a Show with no ESPHome entry (or one whose key
did not work) asks for the key.

**Choosing.** Nothing is shared when a Show is added. The options are a menu over one draft: quick starts that add entities to the list (by
area, by device, by type), the list itself to edit, a helper that copies what Assist may see, what the Show may control, the sensitive actions
(with a PIN), and two extras. *Save* writes the draft; leaving the dialog discards it.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .channel import AuthError, Channel, ChannelError, probe
from .const import (
    CONF_ESPHOME_ENTRY,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_KEY,
    CONF_KEY_SOURCE,
    CONF_MAC,
    CONF_PORT,
    DEFAULT_PORT,
    DOMAIN,
    KEY_SOURCE_ESPHOME,
    KEY_SOURCE_MANUAL,
    MAX_ENTITIES,
    OPT_CAMERA_EVENTS,
    OPT_CONTROL_DOMAINS,
    OPT_ENTITIES,
    OPT_FORWARD_NOTIFICATIONS,
    OPT_PIN_HASH,
    OPT_PIN_SALT,
    OPT_READ_ONLY,
    OPT_SENSITIVE,
    OPT_SHARE_ALARMS,
    OPT_SHARE_STREAMS,
    OPT_SHARE_WEATHER,
    OPT_UNLOCK_CONFIRM_ONLY,
)
from .pin import matches as pin_matches
from .pin import new_pin, well_formed
from .policy import SENSITIVE_ONLY_DOMAINS, confirms_alone, is_sensitive, verbs_of
from .selection import (
    DEFAULT_GROUPS,
    TYPE_GROUPS,
    assist_exposed,
    listed,
    merged,
    pick_by_area,
    pick_by_device,
    pick_by_type,
    summary,
)

ESPHOME_DOMAIN = "esphome"


def esphome_key_for(hass: HomeAssistant, mac: str) -> tuple[str, str] | None:
    """The key of the ESPHome entry of the device with this hardware address, as ``(entry id, base64 key)``."""
    wanted = dr.format_mac(mac)
    for entry in hass.config_entries.async_entries(ESPHOME_DOMAIN):
        if entry.unique_id and dr.format_mac(entry.unique_id) == wanted:
            key = entry.data.get("noise_psk")
            if isinstance(key, str) and key:
                return entry.entry_id, key
    return None


def key_bytes(text: str) -> bytes | None:
    """The 32 bytes of a key written as base64, or ``None`` when the text is not one."""
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == 32 else None


async def _test_key(host: str, port: int, key: str, mac: str) -> str | None:
    """Whether the Show accepts this key: ``None`` if it does, else the error's name."""
    raw = key_bytes(key)
    if raw is None:
        return "invalid_key"
    try:
        channel = await Channel.connect(host, port, raw, expected_mac=mac)
    except AuthError:
        return "invalid_auth"
    except ChannelError:
        return "cannot_connect"
    await channel.close()
    return None


class LumaShowConfigFlow(ConfigFlow, domain=DOMAIN):
    """Adds a Show."""

    VERSION = 1

    def __init__(self) -> None:
        self._host = ""
        self._port = DEFAULT_PORT
        self._mac = ""
        self._name = ""

    # -- finding the Show --------------------------------------------------------------------------------------------------------------

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        props = discovery_info.properties
        mac = str(props.get("mac") or "")
        if len(mac) < 12:
            return self.async_abort(reason="not_lumashow")
        self._host = str(discovery_info.host)
        self._port = discovery_info.port or DEFAULT_PORT
        self._mac = dr.format_mac(mac)
        self._name = str(props.get("friendly_name") or discovery_info.name.partition(".")[0])
        await self.async_set_unique_id(self._mac)
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_with_key()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"name": self._name, "host": self._host},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._port = user_input.get(CONF_PORT, DEFAULT_PORT)
            try:
                name, mac = await probe(self._host, self._port)
            except ChannelError:
                errors["base"] = "cannot_connect"
            else:
                self._mac = dr.format_mac(mac)
                self._name = name
                await self.async_set_unique_id(self._mac)
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
                return await self._async_with_key()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
                }
            ),
            errors=errors,
        )

    # -- the key ---------------------------------------------------------------------------------------------------------------------

    async def _async_with_key(self) -> ConfigFlowResult:
        """The Show's ESPHome entry holds its key: use it. Only when there is none (or it does not work) is the person asked."""
        found = esphome_key_for(self.hass, self._mac)
        if found:
            entry_id, key = found
            result = await _test_key(self._host, self._port, key, self._mac)
            if result is None:
                return self._async_finish(key, KEY_SOURCE_ESPHOME, entry_id)
            if result == "cannot_connect":
                return self.async_abort(reason="cannot_connect")
        return await self.async_step_key()

    async def async_step_key(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_KEY].strip()
            result = await _test_key(self._host, self._port, key, self._mac)
            if result is None:
                return self._async_finish(key, KEY_SOURCE_MANUAL, None)
            errors["base"] = result
        return self.async_show_form(
            step_id="key",
            data_schema=vol.Schema({vol.Required(CONF_KEY): str}),
            description_placeholders={"name": self._name},
            errors=errors,
        )

    @callback
    def _async_finish(self, key: str, source: str, esphome_entry: str | None) -> ConfigFlowResult:
        data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_MAC: self._mac,
            CONF_KEY: key,
            CONF_KEY_SOURCE: source,
            CONF_ESPHOME_ENTRY: esphome_entry,
            CONF_FRIENDLY_NAME: self._name,
        }
        return self.async_create_entry(title=self._name, data=data)

    # -- the key stopped working -----------------------------------------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        self._host = entry.data[CONF_HOST]
        self._port = entry.data[CONF_PORT]
        self._mac = entry.data[CONF_MAC]
        self._name = entry.title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        found = esphome_key_for(self.hass, self._mac)
        if user_input is None and found and found[1] != entry.data[CONF_KEY] and await _test_key(self._host, self._port, found[1], self._mac) is None:
            return self.async_update_reload_and_abort(
                entry,
                data_updates={CONF_KEY: found[1], CONF_KEY_SOURCE: KEY_SOURCE_ESPHOME, CONF_ESPHOME_ENTRY: found[0]},
                reason="reauth_successful",
            )
        if user_input is not None:
            key = user_input[CONF_KEY].strip()
            result = await _test_key(self._host, self._port, key, self._mac)
            if result is None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_KEY: key, CONF_KEY_SOURCE: KEY_SOURCE_MANUAL, CONF_ESPHOME_ENTRY: None},
                    reason="reauth_successful",
                )
            errors["base"] = result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_KEY): str}),
            description_placeholders={"name": self._name},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> LumaShowOptionsFlow:
        return LumaShowOptionsFlow()


# ---------------------------------------------------------------------------------------------------------------------------------
# What the Show is given.


def summary_text(shared: int, controllable: int) -> str:
    """The line the menu opens with."""
    if shared == 0:
        return "Nothing is shared with this Show yet."
    noun = "entity" if shared == 1 else "entities"
    return f"{shared} {noun} shared, {controllable} can be controlled."


def _groups_select(default: list[str] | None = None) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(TYPE_GROUPS),
            multiple=True,
            mode=selector.SelectSelectorMode.LIST,
            translation_key="type_groups",
        )
    )


def _domain_options(domains: list[str]) -> list[selector.SelectOptionDict]:
    return [selector.SelectOptionDict(value=domain, label=domain.replace("_", " ").capitalize()) for domain in domains]


class LumaShowOptionsFlow(OptionsFlowWithReload):
    """Chooses what one Show is given. Every step works on a draft; *Save* writes it."""

    def __init__(self) -> None:
        self._draft: dict[str, Any] | None = None
        self._note = ""

    @property
    def draft(self) -> dict[str, Any]:
        if self._draft is None:
            self._draft = dict(self.config_entry.options)
        return self._draft

    def _add(self, found: list[str], where: str) -> None:
        current = listed(self.draft)
        combined = merged(current, found)
        self.draft[OPT_ENTITIES] = combined[:MAX_ENTITIES]
        added = len(self.draft[OPT_ENTITIES]) - len(current)
        capped = f" (a Show takes at most {MAX_ENTITIES})" if len(combined) > MAX_ENTITIES else ""
        self._note = f"Added {added} from {where}{capped}." if found else f"Nothing to add from {where}."

    # -- the menu ----------------------------------------------------------------------------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        shared, controllable = summary(self.hass, self.draft)
        text = summary_text(shared, controllable)
        if self._note:
            text = f"{self._note} {text}"
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "pick_area",
                "pick_device",
                "pick_type",
                "pick_entities",
                "copy_assist",
                "control",
                "sensitive",
                "pin",
                "extras",
                "save",
            ],
            description_placeholders={"summary": text},
        )

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> Any:
        return self.async_create_entry(data=self.draft)

    # -- quick starts -------------------------------------------------------------------------------------------------------------------

    async def async_step_pick_area(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            found = pick_by_area(
                self.hass,
                user_input["areas"],
                user_input.get("types") or DEFAULT_GROUPS,
                user_input.get("include_minor", False),
            )
            self._add(found, "these areas")
            return await self.async_step_init()
        return self.async_show_form(
            step_id="pick_area",
            data_schema=vol.Schema(
                {
                    vol.Required("areas"): selector.AreaSelector(selector.AreaSelectorConfig(multiple=True)),
                    vol.Optional("types", default=list(DEFAULT_GROUPS)): _groups_select(),
                    vol.Optional("include_minor", default=False): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_pick_device(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            found = pick_by_device(
                self.hass,
                user_input["devices"],
                user_input.get("types") or DEFAULT_GROUPS,
                user_input.get("include_minor", False),
            )
            self._add(found, "these devices")
            return await self.async_step_init()
        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema(
                {
                    vol.Required("devices"): selector.DeviceSelector(selector.DeviceSelectorConfig(multiple=True)),
                    vol.Optional("types", default=list(DEFAULT_GROUPS)): _groups_select(),
                    vol.Optional("include_minor", default=False): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_pick_type(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            found = pick_by_type(
                self.hass,
                user_input["groups"],
                user_input.get("areas") or None,
                user_input.get("include_minor", False),
            )
            self._add(found, "these kinds")
            return await self.async_step_init()
        return self.async_show_form(
            step_id="pick_type",
            data_schema=vol.Schema(
                {
                    vol.Required("groups"): _groups_select(),
                    vol.Optional("areas"): selector.AreaSelector(selector.AreaSelectorConfig(multiple=True)),
                    vol.Optional("include_minor", default=False): selector.BooleanSelector(),
                }
            ),
        )

    async def async_step_copy_assist(self, user_input: dict[str, Any] | None = None) -> Any:
        exposed = assist_exposed(self.hass)
        if user_input is not None:
            if user_input.get("add"):
                self._add(exposed, "Assist")
            return await self.async_step_init()
        return self.async_show_form(
            step_id="copy_assist",
            data_schema=vol.Schema({vol.Required("add", default=False): selector.BooleanSelector()}),
            description_placeholders={"count": str(len(exposed))},
        )

    # -- the list ------------------------------------------------------------------------------------------------------------------------

    async def async_step_pick_entities(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            chosen = list(dict.fromkeys(user_input.get(OPT_ENTITIES) or []))[:MAX_ENTITIES]
            self.draft[OPT_ENTITIES] = chosen
            # What was taken off the list is taken off every other list that names it.
            for key in (OPT_READ_ONLY, OPT_SENSITIVE):
                self.draft[key] = [e for e in self.draft.get(key) or [] if e in chosen]
            self._note = f"The list has {len(chosen)}."
            return await self.async_step_init()
        return self.async_show_form(
            step_id="pick_entities",
            data_schema=vol.Schema(
                {vol.Optional(OPT_ENTITIES, default=listed(self.draft)): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True))}
            ),
        )

    # -- control ---------------------------------------------------------------------------------------------------------------------------

    def _controllable_domains(self) -> list[str]:
        """The domains on the list that have verbs a Show may use without a PIN."""
        domains = {e.partition(".")[0] for e in listed(self.draft)}
        return sorted(d for d in domains if d not in SENSITIVE_ONLY_DOMAINS and any(not is_sensitive(d, v) for v in verbs_of(d)))

    async def async_step_control(self, user_input: dict[str, Any] | None = None) -> Any:
        domains = self._controllable_domains()
        if user_input is not None:
            self.draft[OPT_CONTROL_DOMAINS] = [d for d in user_input.get(OPT_CONTROL_DOMAINS) or [] if d in domains]
            self.draft[OPT_READ_ONLY] = [e for e in user_input.get(OPT_READ_ONLY) or [] if e in listed(self.draft)]
            self._note = "Control saved in the draft."
            return await self.async_step_init()
        chosen = [d for d in self.draft.get(OPT_CONTROL_DOMAINS) or [] if d in domains]
        return self.async_show_form(
            step_id="control",
            data_schema=vol.Schema(
                {
                    vol.Optional(OPT_CONTROL_DOMAINS, default=chosen): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=_domain_options(domains),
                            multiple=True,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    ),
                    vol.Optional(
                        OPT_READ_ONLY, default=[e for e in self.draft.get(OPT_READ_ONLY) or [] if e in listed(self.draft)]
                    ): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, include_entities=listed(self.draft) or ["none.none"])),
                }
            ),
        )

    async def async_step_sensitive(self, user_input: dict[str, Any] | None = None) -> Any:
        # Without a PIN only the confirmation-only unlock of a lock can be allowed (Notifications, cameras and alarms > Unlock from this device
        # with a confirmation only); every other sensitive action needs the PIN to exist.
        confirm_only = bool(self.draft.get(OPT_UNLOCK_CONFIRM_ONLY))
        if not self.draft.get(OPT_PIN_HASH) and not confirm_only:
            self._note = "Set a PIN first: a sensitive action cannot be allowed without one."
            return await self.async_step_init()
        pin_set = bool(self.draft.get(OPT_PIN_HASH))
        candidates = [
            e
            for e in listed(self.draft)
            if any(
                is_sensitive(e.partition(".")[0], v) and (pin_set or confirms_alone(e.partition(".")[0], v)) for v in verbs_of(e.partition(".")[0])
            )
        ]
        if user_input is not None:
            self.draft[OPT_SENSITIVE] = [e for e in user_input.get(OPT_SENSITIVE) or [] if e in candidates]
            self._note = "Sensitive actions saved in the draft."
            return await self.async_step_init()
        return self.async_show_form(
            step_id="sensitive",
            data_schema=vol.Schema(
                {
                    vol.Optional(OPT_SENSITIVE, default=[e for e in self.draft.get(OPT_SENSITIVE) or [] if e in candidates]): selector.EntitySelector(
                        selector.EntitySelectorConfig(multiple=True, include_entities=candidates or ["none.none"])
                    )
                }
            ),
            description_placeholders={"count": str(len(candidates))},
        )

    async def async_step_pin(self, user_input: dict[str, Any] | None = None) -> Any:
        errors: dict[str, str] = {}
        has_pin = bool(self.draft.get(OPT_PIN_HASH))
        if user_input is not None:
            new, again = (user_input.get("new_pin") or "").strip(), (user_input.get("confirm_pin") or "").strip()
            current = (user_input.get("current_pin") or "").strip()
            if has_pin and not await self.hass.async_add_executor_job(pin_matches, current, self.draft):
                errors["current_pin"] = "pin_wrong"
            elif new and not well_formed(new):
                errors["new_pin"] = "pin_format"
            elif new != again:
                errors["confirm_pin"] = "pin_mismatch"
            if not errors:
                if new:
                    hashed, salt = await self.hass.async_add_executor_job(new_pin, new)
                    self.draft[OPT_PIN_HASH], self.draft[OPT_PIN_SALT] = hashed, salt
                    self._note = "The PIN is set."
                else:
                    # An empty new PIN removes it, and with it every sensitive action that needed it.
                    for key in (OPT_PIN_HASH, OPT_PIN_SALT):
                        self.draft.pop(key, None)
                    self.draft[OPT_SENSITIVE] = []
                    self._note = "The PIN is removed, and the sensitive actions with it."
                return await self.async_step_init()
        password = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
        schema: dict[Any, Any] = {}
        if has_pin:
            schema[vol.Required("current_pin")] = password
        schema[vol.Optional("new_pin", default="")] = password
        schema[vol.Optional("confirm_pin", default="")] = password
        return self.async_show_form(
            step_id="pin", data_schema=vol.Schema(schema), errors=errors, description_placeholders={"set": "set" if has_pin else "not set"}
        )

    async def async_step_extras(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            self.draft[OPT_FORWARD_NOTIFICATIONS] = bool(user_input.get(OPT_FORWARD_NOTIFICATIONS))
            self.draft[OPT_SHARE_STREAMS] = bool(user_input.get(OPT_SHARE_STREAMS))
            self.draft[OPT_SHARE_ALARMS] = bool(user_input.get(OPT_SHARE_ALARMS))
            self.draft[OPT_CAMERA_EVENTS] = bool(user_input.get(OPT_CAMERA_EVENTS))
            self.draft[OPT_SHARE_WEATHER] = bool(user_input.get(OPT_SHARE_WEATHER))
            self.draft[OPT_UNLOCK_CONFIRM_ONLY] = bool(user_input.get(OPT_UNLOCK_CONFIRM_ONLY))
            self._note = "Saved in the draft."
            return await self.async_step_init()
        return self.async_show_form(
            step_id="extras",
            data_schema=vol.Schema(
                {
                    vol.Optional(OPT_FORWARD_NOTIFICATIONS, default=bool(self.draft.get(OPT_FORWARD_NOTIFICATIONS))): selector.BooleanSelector(),
                    vol.Optional(OPT_SHARE_STREAMS, default=bool(self.draft.get(OPT_SHARE_STREAMS))): selector.BooleanSelector(),
                    vol.Optional(OPT_SHARE_ALARMS, default=bool(self.draft.get(OPT_SHARE_ALARMS))): selector.BooleanSelector(),
                    vol.Optional(OPT_CAMERA_EVENTS, default=bool(self.draft.get(OPT_CAMERA_EVENTS))): selector.BooleanSelector(),
                    vol.Optional(OPT_SHARE_WEATHER, default=bool(self.draft.get(OPT_SHARE_WEATHER))): selector.BooleanSelector(),
                    vol.Optional(OPT_UNLOCK_CONFIRM_ONLY, default=bool(self.draft.get(OPT_UNLOCK_CONFIRM_ONLY))): selector.BooleanSelector(),
                }
            ),
        )
