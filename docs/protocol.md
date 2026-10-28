# The LumaShow ↔ Home Assistant protocol, version 1

How a Show and Home Assistant's **LumaShow integration** (`lumashow-ha`, a HACS custom integration) talk. The design behind it is
`docs/lumashow-ha-integration.md`; the code is `crates/ha-bridge` (the Show) and `custom_components/lumashow` (Home Assistant). This file
is the contract between the two repositories: both hold a copy, and `crates/ha-bridge/tests/vectors.json` (the same file as
`tests/vectors/protocol.json` in `lumashow-ha`) is decoded by both test suites.

**The one rule.** Home Assistant pushes what the person chose to share with *this* Show, and nothing else. The Show asks for actions;
the integration, not the Show, decides. The Show holds no Home Assistant address and no token, and cannot ask Home Assistant for
anything that is not in a message below.

Words: *the Show* is the server (it listens), *the integration* is the client (it dials). Every number is a JSON number, every text UTF-8.

## 1. Transport

| | |
|---|---|
| Port | TCP **6054** on the Show. Open only while Settings › Home Assistant › *LumaShow integration* is on **and** the Show has a pairing key. |
| Announcement | mDNS `_lumashow._tcp.local.`, instance `lumashow-<last six hex digits of the MAC>`. TXT records: `mac` (12 lower-case hex), `proto` (highest protocol version), `friendly_name`, `project_name`, `project_version`, `modules` (comma list), `api_encryption` (`Noise_NNpsk0_25519_ChaChaPoly_SHA256`). TXT is for discovery only; nothing in it is trusted. |
| Cipher | `Noise_NNpsk0_25519_ChaChaPoly_SHA256`, prologue `"NoiseAPIInit" 00 00`, and the **framing of ESPHome's encrypted API** (hello frames, the 48-byte handshake messages, `0x01`+length frames), so the Show's `esphome-api` serves both. |
| Key | `PSK = HMAC-SHA256(key = pairing_key, message = "LumaShow HA bridge v1")`. `pairing_key` is the 32-byte key of the **same Show's ESPHome device**: Home Assistant already holds it in the ESPHome config entry (`noise_psk`, base64), so the person pairs once and types nothing. The derivation keeps the two protocols apart (a handshake made for one never opens the other) while one pairing serves both. |
| Roles | The integration dials; the Show never dials out. One authenticated session at a time: a new one replaces the old (the old is sent `bye` with reason `replaced`). At most 4 connections that have not finished the handshake and `hello`; each has 10 s. |

Inside a Noise transport frame the plaintext is `type: u16 BE`, `length: u16 BE`, then `length` bytes. **Type 1** is a message (a JSON
object). Every other type is reserved for binary modules and is ignored (never an error). A message is at most **48 KiB**.

## 2. The envelope and the rules of extension

```json
{"m": "states", "t": "update", "...": "fields of this message"}
```

`m` is the module, `t` the message in it. Rules, for both ends:

1. **Unknown fields are ignored.** A field is added, never changed in meaning.
2. **Unknown modules and unknown messages of a known module are ignored** (not an error, not logged above debug).
3. A module that was not agreed in `welcome` is off: its messages are ignored in both directions.
4. A message that is not JSON, not an object, or lacks `m` or `t` is answered with `core.error` (`bad_message`); three in a row end the session.
5. Strings are at most 200 characters unless a field says otherwise; a longer one is cut by the sender, refused by the receiver.

**Versions.** `proto` is the protocol's integer; a change of meaning of an existing message bumps it, an addition does not. A module has an integer
version of its own, compatible within it. The agreed version of a module is the lower of the two ends'.

## 3. `core` (always on)

| Message | Direction | Fields |
|---|---|---|
| `hello` | integration → Show | `proto` (list of supported versions, ascending), `client` {`name`, `version`, `ha_version`, `instance` (HA's UUID), `entry` (the config entry's id)}, `modules` {name: highest version it speaks} |
| `welcome` | Show → integration | `proto` (the one chosen), `device` {`name`, `friendly_name`, `mac`, `manufacturer`, `model`, `sw_version`, `display` {`width`,`height`}, `has` {`camera`,`microphone`,`speaker`}}, `modules` {name: agreed version}, `limits` {`message_bytes`, `entities`, `call_entities`} |
| `ping` / `pong` | either | `n` (the pong repeats it). The integration pings every 25 s of quiet; each end drops a session that has been silent for 75 s. |
| `error` | either | `code`, `message`; codes: `bad_message`, `too_large`, `proto_unsupported`, `internal`. Fatal ones are followed by a close. |
| `bye` | either | `reason`: `unloaded`, `disabled`, `removed`, `reconfigured`, `shutdown`, `replaced`, `unpaired` (the Show was told to forget Home Assistant: its key is gone). The sender closes; **the receiver drops everything the other side had given it** (the Show clears every entity, camera and notification at once). |

The Show answers a `hello` with no common `proto` with `error`/`proto_unsupported` (listing what it speaks) and closes.

## 4. `states` (module version 1): what the person shared

The Show holds a mirror of exactly the entities in the last `snapshot` and keeps it current from `update`. It has no other source: when
the session ends the mirror is dropped.

**`snapshot`** (integration → Show) replaces the mirror. Large ones are sent in parts (each part at most 32 KiB); the Show applies the
set when the part with `more: false` arrives (and drops a half-received set after 10 s, then sends `resync`).

```json
{"m":"states","t":"snapshot","rev":7,"part":0,"more":false,
 "policy":{"paused":false,"pin_set":true},
 "areas":[{"id":"kitchen","name":"Kitchen"}],
 "entities":[
  {"id":"light.kitchen_main","name":"Kitchen main light","area":"kitchen","state":"on","attrs":{"brightness":180,"friendly_name":"Kitchen main light"},
   "changed":1792520000.5,"ctl":["turn_on","turn_off","toggle"]},
  {"id":"lock.front_door","name":"Front door","state":"locked","attrs":{"device_class":"lock"},"ctl":["lock"],"ctl_pin":["unlock"]},
  {"id":"sensor.hall_temperature","name":"Hall temperature","area":"hall","state":"21.4","attrs":{"unit_of_measurement":"°C","device_class":"temperature"},"minor":false}]}
```

| Field | Meaning |
|---|---|
| `rev` | Counts every change of the shared set or of the policy; only for ordering and logs. |
| `policy.paused` | Control is paused in Home Assistant: every `ctl` below is empty. `policy.pin_set`: a PIN exists (a sensitive verb may be offered). |
| `areas` | **Only** the areas of the shared entities (a name beyond them is never sent). |
| `entities[]` | `id` (`domain.object_id`), `name` (what a person calls it), `area` (an id of `areas`, absent when none), `state` (text; `unavailable`, `unknown` included), `attrs` (the allowed attributes of its domain, §9), `changed` (seconds since the epoch of the last change of the state), `minor` (true: hidden, configuration or diagnostic: pickers leave it out), `ctl` (the verbs the Show may use on it **now**, §6), `ctl_pin` (verbs that need the PIN and an on-screen confirmation). |

**`update`** (integration → Show): `{"m":"states","t":"update","rev":8,"entities":[{"id":…,"state":…,"attrs":{…},"changed":…}],"removed":["light.old"]}`. Only
state, attributes and `changed` of entities already in the mirror; a change of name, area, `ctl` or of the set is a new `snapshot`. Sent
at most four times a second per Show, an entity at most once per change of its sent value (a rewrite with the same state and attributes
is not sent). An update for an entity not in the mirror is ignored.

**`resync`** (Show → integration): ask for a fresh `snapshot`.

## 5. `actions` (module version 1)

**`call`** (Show → integration): `{"m":"actions","t":"call","op":12,"entity_ids":["light.a","light.b"],"verb":"turn_on","args":{"brightness_pct":40},"origin":"plugin"}`

- `entity_ids`: 1–16 entities **of one domain**; `verb`: a name of §6 for that domain; `args`: only the keys §6 lists for the verb;
- `origin`: `touch` (a finger on the Show's own screen, nothing else), `plugin`, `voice`, `automation`. Anything else is `plugin`. A sensitive verb is allowed from `touch` only;
- `confirmed` (true when the person pressed the on-screen confirmation just now) and `pin` (the digits they typed): present only on a sensitive call.

**`result`** (integration → Show): `{"m":"actions","t":"result","op":12,"ok":false,"code":"not_allowed","message":"light.b is not shared with this Show","results":[{"entity_id":"light.a","ok":true},{"entity_id":"light.b","ok":false,"code":"not_allowed"}]}`.
`ok` is true only when every entity was done. Codes: `unknown_verb`, `bad_args`, `not_allowed` (not shared, read-only, or the verb is not in its
list), `paused`, `rate_limited`, `sensitive_disabled`, `needs_touch`, `needs_confirm`, `pin_required`, `pin_wrong`, `locked_out`, `unavailable`, `failed`, `timeout`, `too_many`.
The integration answers every `call` exactly once, within 15 s.

Nothing else is ever executed: **there is no free-form service call**, no target, no template, no data key outside the verb's list.

## 6. The verbs

A verb is the name of the Home Assistant service of the entity's own domain, **and only these exist**. The integration holds this table
(`actions.py`) and refuses anything else (`unknown_verb`); the Show may use it to hide controls. `S` marks a sensitive verb: never
allowed unless the person enabled it for that entity in Home Assistant, and then only with `origin: touch`, `confirmed: true` and the PIN.

| Domain | Verbs (args) |
|---|---|
| `light` | `turn_on` (`brightness_pct` 0-100, `brightness` 0-255, `rgb_color` [r,g,b], `color_temp_kelvin` 1000-12000, `transition` 0-300, `effect` ≤ 64 chars), `turn_off` (`transition`), `toggle` |
| `switch`, `input_boolean` | `turn_on`, `turn_off`, `toggle` |
| `fan` | `turn_on` (`percentage`, `preset_mode`), `turn_off`, `toggle`, `set_percentage` (`percentage`), `set_preset_mode`, `oscillate` (`oscillating`), `increase_speed`, `decrease_speed` |
| `cover` | `open_cover`, `close_cover`, `stop_cover`, `toggle`, `set_cover_position` (`position` 0-100), `open_cover_tilt`, `close_cover_tilt`, `set_cover_tilt_position` (`tilt_position`). **S** for `device_class` `garage`, `gate`, `door`: `open_cover`, `toggle`, `set_cover_position`, `open_cover_tilt` |
| `climate` | `set_temperature` (`temperature`, `target_temp_high`, `target_temp_low`, `hvac_mode`), `set_hvac_mode`, `set_preset_mode`, `set_fan_mode`, `turn_on`, `turn_off` |
| `media_player` | `media_play`, `media_pause`, `media_play_pause`, `media_stop`, `media_next_track`, `media_previous_track`, `volume_set` (`volume_level` 0-1), `volume_up`, `volume_down`, `volume_mute` (`is_volume_muted`), `turn_on`, `turn_off`, `select_source` (`source`) |
| `scene` | `turn_on` |
| `script` | `turn_on` (only an entity the person picked by name; never by area, device or type) |
| `automation` | `trigger` (as for a script) |
| `button`, `input_button` | `press` |
| `number`, `input_number` | `set_value` (`value`) |
| `select`, `input_select` | `select_option` (`option`) |
| `humidifier` | `turn_on`, `turn_off`, `set_humidity` (`humidity`), `set_mode` (`mode`) |
| `vacuum` | `start`, `pause`, `stop`, `return_to_base`, `locate` |
| `lock` | `lock`; **S**: `unlock`, `open` |
| `alarm_control_panel` | `alarm_arm_home`, `alarm_arm_away`, `alarm_arm_night`, `alarm_arm_vacation`; **S**: `alarm_disarm` |
| `valve` | `close_valve`; **S**: `open_valve`, `set_valve_position`, `toggle` |
| `camera` | `turn_on`, `enable_motion_detection`; **S**: `turn_off`, `disable_motion_detection` |
| `siren` | **S**: `turn_on`, `turn_off`, `toggle` |

Any entity whose `device_class` is a safety one (`smoke`, `carbon_monoxide`, `gas`, `moisture`, `safety`, `tamper`, `problem`) has every
verb **S** (turning off a smoke detector's switch is not an everyday action). Every other domain has no verbs. Values are plain
(numbers, booleans, text of at most 64 characters with no control characters, a list of at most 4 numbers) and are range-checked.

## 7. `cameras` (module version 1)

**`list`** (integration → Show): the shared cameras (entities of the `camera` domain in the shared set), `{"m":"cameras","t":"list","rev":3,"partial":false,
"cameras":[{"id":"camera.front_door","name":"Front door","area":"entry","snapshot":{"url":"https://…/api/camera_proxy/camera.front_door?authSig=…","expires":1792523600},
"streams":[{"kind":"rtsp","url":"rtsp://…"}]}]}`.

- `snapshot.url` is a **signed, expiring** address of Home Assistant's picture of the camera: it carries no token and opens nothing else.
  `expires` is in seconds since the epoch; the integration sends a new list before it runs out.
- `streams` is present only when the person turned on *Share camera addresses* for this Show (default off: an address may carry a password).
  `kind` is `rtsp`, `hls`, `mjpeg` or `webrtc`.
- `partial: true` replaces only the cameras it lists.

**`refresh`** (Show → integration): `{"m":"cameras","t":"refresh","ids":["camera.front_door"]}` asks for fresh signed addresses (answered by a partial `list`).

## 8. `notify` (module version 1)

- `snapshot` (integration → Show): `{"m":"notify","t":"snapshot","items":[{"id":"…","title":"…","message":"…","level":"info","source":"persistent_notification"}]}` the notifications standing now (replaces what the Show holds from here).
- `post`: one more or one updated: `{"m":"notify","t":"post","id":"…","title":"…","message":"…","level":"info|warning|critical","ttl_s":3600,"source":"service|persistent_notification"}`. `title` ≤ 200 and `message` ≤ 1000 characters. `ttl_s` optional (the Show may drop it after that).
- `clear`: `{"m":"notify","t":"clear","id":"…"}`.
- `dismissed` (Show → integration): the person dismissed `id` on the Show (Home Assistant may clear its persistent notification; v1 integration only counts it).

Home Assistant's own persistent notifications are sent **only** when the person turned that on for this Show (default off); the `lumashow.notify`
service and the Show's `notify` entity send `source: service`.

## 9. Attributes

An entity carries only the attributes its domain lists here (so a light's `supported_color_modes`, a media player's long queues or a camera's `access_token` never leave Home Assistant):

| Domain | Attributes |
|---|---|
| every | `friendly_name`, `device_class`, `icon`, `unit_of_measurement`, `state_class` |
| `light` | `brightness`, `color_mode`, `color_temp_kelvin`, `rgb_color`, `supported_color_modes` (list ≤ 8), `effect` |
| `event` | `event_type`, `event_types` (list ≤ 8) |
| `cover` | `current_position`, `current_tilt_position` |
| `climate` | `current_temperature`, `temperature`, `target_temp_high`, `target_temp_low`, `hvac_action`, `hvac_modes`, `preset_mode` |
| `media_player` | `media_title`, `media_artist`, `volume_level`, `is_volume_muted`, `source` |
| `fan` | `percentage`, `preset_mode`, `oscillating` |
| `sensor`, `binary_sensor` | (the common ones only) |

A text longer than 128 characters is dropped, a list longer than 16 entries is dropped. The Show keeps the subset its widgets use today
(`friendly_name`, `device_class`, `event_type`, `brightness`, `icon`, `unit_of_measurement`) and may keep more later.

## 10. `alarms` (module version 1) and the reserved modules

`media`, `presence` and `intercom` are **reserved names**: no message is defined and both ends ignore them. A module is added by listing its name in `hello`/`welcome`
with a version; nothing else changes. `alarms` is defined here: how the alarm clocks of several Shows are kept in step, and who rings when only one device should.

**Offered only when the person turned on *Share alarms* for that Show** (an option of its entry; nothing is shared by default), and only while the Show registered a handler.
The integration is a **store with compare-and-swap and a relay**; every merge is the Show's. The design is `docs/alarms.md` in the LumaShow repository; the messages and their
shared test vectors are `crates/alarm-sync/src/wire.rs` and `tests/vectors/alarms.json` (the same file as `crates/alarm-sync/tests/vectors.json`).

**The store.** One Local Calendar, `calendar.lumashow_alarms` (the person adds it once: Local Calendar, named *LumaShow alarms*). One event per alarm: the *summary* is the label
(`(off) ` in front when it is off), the start's time of day is the time, the recurrence rule is the days (`FREQ=DAILY`, `FREQ=WEEKLY;BYDAY=MO,TU,...`; none: a once-only alarm, whose
date is the start's), and the first lines of the *description* are `volume: 80`, `sound: dawn`, `ring: one` and `edited: <seconds> <device>` (a missing line is the default; anything
else a person wrote is kept). A person may edit an event in Home Assistant's own calendar panel. An event with a rule a Show cannot keep (monthly, every other day, a count or an
end) is not an alarm; it stays in the calendar and is not sent. The calendar is read from 14 days back to 10 days ahead: a once-only alarm whose day is further back than that has
rung and leaves the Shows, and one for a day further ahead than that reaches them when it is that near.

**`rev`.** Every alarm has a `rev` that rises with every accepted write **and with every edit a person makes in the calendar** (the integration notices the difference). A person's edit
leaves the `edited` stamp alone, which is how a Show tells it from another Show's. The set has a `rev` too (in `snapshot`) that rises with every change of anything the snapshot says
(an alarm, the zone, whether the calendar can be read) and never falls, not even across a restart of Home Assistant; **a Show ignores a snapshot older than one it has**, because messages
can cross.

| Message | From | Fields |
|---|---|---|
| `snapshot` | integration | `rev`, `tz` (Home Assistant's time zone: the alarms' times are wall-clock times in it), `ready` (false while the calendar cannot be read: nothing is to be believed deleted), `alarms`: [`uid`, `rev`, `label`, `time` "HH:MM", `days` ["mon".."sun"] (none: once), `date` (once only), `on`, `volume` 1-100, `sound`?, `ring` "every" or "one", `by` (the Show that last edited it, "" if none), `at`] |
| `result` | integration | `op`, then `ok: true, uid, rev` or `ok: false, code, message` (`conflict` adds `uid` and `current`: the alarm as it is now, `null` if gone; `bad_alarm`, `too_many`, `unavailable`) |
| `beacon` | integration | `from`, `age` (seconds since that Show last sensed a person, or null), `prio` |
| `answered` | integration | `from`, `occ`, `how` ("stopped" or "snoozed") |
| `put` | Show | `op`, `uid` (the Show's choice: `ls-` and 12 hex digits), `expect` (the `rev` the change is based on; 0 creates), `alarm` (the alarm's fields as above, without `uid`, `rev`, `by`), `at` |
| `delete` | Show | `op`, `uid`, `expect` |
| `beacon`, `answered` | Show | as above, without `from` |
| `resync` | Show | send the snapshot again |

**Compare-and-swap.** A `put` or `delete` is accepted only when `expect` is the alarm's current `rev` (the calendar is read again first, so that an edit a person made a moment ago counts);
otherwise the answer is `conflict` with the alarm as it is now and the Show merges and writes again. A write is stamped with the session's device name, never one the Show gave. At most 32 alarms
(`too_many`). After every accepted change every Show that shares alarms gets a `snapshot`, the writer too.

**The relay.** A Show's `beacon` and `answered` go to every *other* Show that shares alarms, with `from` set by the integration. An `answered` is also fired as the Home Assistant event
`lumashow_alarm` (`kind: "answered"`, `from`, `occ`, `how`), so that an automation can ring what the Shows cannot (a speaker, a phone) when nobody answered. Beacons are not events.

## 10b. `camera_events` (module version 1)

What a shared camera's detectors see, as it happens, for a device that shows a pop-up over whatever is playing (a LumaTV) or a card. **Offered only when the
person turned on *Send camera events* for that device** (an option of its entry; off by default), and only while the device registered a handler. The
integration owns the mapping from detectors to cameras, so a device never sees a detector entity the person did not share, and needs no registry.

- **Sources.** For each camera of the shared set: the `binary_sensor` and `event` entities of the camera entity's own device whose name or class says what they
  detect (`custom_components/lumashow/camera_events.py`, `classify`), and `event` entities elsewhere whose `camera` attribute is the camera's name. Hidden and
  disabled entities never count. The kinds are `person`, `vehicle`, `animal`, `package`, `doorbell` and `motion`; an event entity that announces several classes
  (`event_type` `person`, `vehicle`, ...) is read at each event.
- **One event, once.** A binary sensor fires when it turns on, an event entity when it has a new moment; two of one camera and one kind within 2 s are one event.

| Message | Direction | Fields |
|---|---|---|
| `list` | integration → device | `cameras`: [`id` (the camera entity), `kinds` (the kinds its detectors can announce, in the order above)]. Sent when the module's session begins and when the shared set changes. |
| `event` | integration → device | `camera` (the camera entity, one of `cameras.list`), `kind`, `at` (seconds since the epoch of the detection), `source` (the detector entity), `label` (optional, at most 64 characters: what the detector called it, a species or a class). |
| `refresh` | device → integration | asks for `list` again. |

The camera's picture is the one `cameras.list` already gave (`snapshot.url`): a device fetches it when an event arrives and shows it with the words.

## 11. Who decides what (the rules both ends must keep)

1. The **integration** decides every action: entity shared with this Show, control allowed, verb in §6, args in range, not paused, rate limit (10 calls in 10 s per Show), sensitive rules.
2. The Show cannot read what was not pushed: no registry, no `get_states`, no history, no other entity, no other area or device name.
3. **Sensitive** verbs: refused (`sensitive_disabled`) unless that entity has them enabled in Home Assistant; then they need `origin: touch`, `confirmed: true` and the PIN the person set in the integration (its hash lives in Home Assistant and is checked there; five wrong PINs lock sensitive verbs for 10 minutes). A voice, plugin or automation origin never opens them.
4. Every call is in Home Assistant's **logbook** as `<Show name>: <what>` with the entity, counted by an entity, and fired as the `lumashow_action` event; every refusal is logged. A *Pause control* switch per Show refuses every call at once.
5. Unloading, disabling or removing the config entry sends `bye` and closes: the Show drops everything at once. Connecting again after the Show has forgotten Home Assistant needs a new key, and only the Show's Pair window (five minutes, opened on the Show) gives one.
