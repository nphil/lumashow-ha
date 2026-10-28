# LumaShow for Home Assistant

[![Open your Home Assistant instance and open this repository in the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nphil&repository=lumashow-ha&category=integration)

Lets your **LumaShow** screens (the first is an Echo Show 5 running LumaShow OS) see and control **only the things you pick** in Home Assistant.

**Public home:** <https://github.com/nphil/lumashow-ha>. Releases, issues and HACS are served from there.

- **You choose, per Show.** Areas, devices, kinds of thing (lights, locks, cameras...) or single entities. A new Show starts with **nothing**.
- **Home Assistant sends only that.** The Show holds **no access token and no address of your Home Assistant**. It cannot ask for anything that is not on its list, and it learns the name of an area only if one of its entities stands there.
- **You choose what it may do.** By default it may only look. You tick the kinds it may act on (turn lights on and off, close covers, set a thermostat...). Home Assistant has a fixed list of actions per kind; the Show can ask for one of those and for nothing else.
- **Dangerous actions are off.** Unlocking, disarming an alarm, opening a garage door, a gate or a door, opening a valve, switching a camera or a siren off: each needs you to turn it on for that entity, a PIN you set (kept as a hash here; the Show never has it), and a fresh touch and confirmation on the Show. Voice, plugins and automations can never do them.
- **You can see and stop it.** Every action a Show makes is in the Logbook (*"Living Room Show: turned on Kitchen light"*), counted by a sensor, held to 10 actions in 10 seconds, and one switch per Show, **Pause control**, stops all of them at once.

This is the Home Assistant half. The Show half is part of LumaShow OS (*Settings › Home Assistant* on the Show). The wire protocol both speak is in [`docs/protocol.md`](docs/protocol.md).

## What you need

- Home Assistant **2026.9** or newer (tested with 2026.9 and 2026.10), with [HACS](https://hacs.xyz) 2 (or copy the folder by hand, below).
- A LumaShow device on a LumaShow OS update that has *Settings › Home Assistant*.
- Home Assistant's own **ESPHome** integration. It is what pairs the Show (it makes the key); this integration reuses that key, so you type nothing.
- The Show and Home Assistant on the same network. **Home Assistant dials the Show** (TCP port 6054, announced as `_lumashow._tcp`); the Show never calls out. Nothing is opened on your router.

## Install with HACS

1. In Home Assistant: **HACS**, the three dots at the top right, **Custom repositories**.
2. *Repository:* `https://github.com/nphil/lumashow-ha`, *Type:* **Integration**, **Add**. (Or press the badge at the top of this page.)
3. Find **LumaShow** in HACS, **Download**, then **restart Home Assistant**.

Without HACS: copy the folder `custom_components/lumashow` of the [latest release](https://github.com/nphil/lumashow-ha/releases) into `/config/custom_components/` and restart.

## Pair the Show (once)

1. **On the Show:** *Settings › Home Assistant*, touch **Pair with Home Assistant** and confirm. A window of five minutes opens. (The Show's voice does not need to be on, and stays off: while the window is open the Show listens for Home Assistant alone, no microphone, and stops listening when it shuts.)
2. **In Home Assistant**, within the five minutes: *Settings › Devices & services*. The Show is offered as a discovered **ESPHome** device: press **Add**. Home Assistant makes the key itself and hands it to the Show. That is the only pairing there is. (Once it has a key the Show keeps this ESPHome connection open with the voice off too, so Home Assistant's entry for it stays available. While the voice is off the Show tells Home Assistant it has no voice assistant and no media player; when you turn the voice on it reconnects and tells Home Assistant again. No microphone is listening while the voice is off.)
3. A moment later the Show also appears as **Discovered: LumaShow ...**: press **Add**. The key is taken from the Show's ESPHome device; nothing is typed. (If it does not appear: *Add integration › LumaShow* and give the Show's address. The key is asked for only if there is no ESPHome device for that Show.)

## Pick what the Show is given

On the LumaShow entry press **Configure**. The first line always says what you have (*"Nothing is shared with this Show yet."*, later *"12 entities shared, 7 can be controlled."*).

| Menu entry | What it does |
|---|---|
| *Add by area*, *Add by device*, *Add by type* | Each **adds** what it finds to one list, once. Nothing follows later: a device you put in the kitchen next month is not shared until you add it. Hidden, diagnostic and configuration entities are skipped unless you ask. |
| *Edit the list, one entity at a time* | The list is exactly what the Show is given. Take anything off, add anything by name. |
| *Copy what Assist can see (optional)* | A helper you may use once. It is never a default and nothing follows Assist afterwards. |
| *What the Show may control* | Tick the kinds of thing it may act on; hold single entities read-only. |
| *Sensitive actions*, *PIN* | Only if you want them (see below). |
| *Notifications, cameras and alarms* | What else the Show is given. All off by default. |
| *Save* | Applies it. No restart. |

At most 256 entities per Show.

## The defaults: the least it can do

| | Default |
|---|---|
| What the Show sees | **Nothing.** The list starts empty. |
| What the Show may do | **Nothing, only look.** You tick the kinds it may act on. |
| Which actions exist | A fixed table per kind of entity (a light's on, off, brightness, colour; a cover's open, close, position; a thermostat's setpoint and mode...). There is no free-form service call. An unknown action is refused and logged. |
| Scripts and automations | Never added by a quick start. Shared only if you pick them by name. |
| Sensitive actions (unlock, disarm, open a garage door, gate or door, open a valve, switch a camera or siren off, anything on a smoke or leak sensor's switch) | **Off.** Allowed per entity, then they need your PIN, a touch on the Show and a confirmation there. Five wrong PINs lock them for ten minutes. |
| Camera pictures | A temporary signed address (valid for an hour) for each shared camera. The camera's stream address, which can contain a password, is **not** sent unless you turn it on. |
| Home Assistant's own notifications | **Off.** |
| Alarms | **Off** (see below). |
| Camera events | **Off**. On: a shared camera's detectors (person, vehicle, animal, package, doorbell, motion) are sent as they happen, so a LumaTV can show who is at the door over whatever is playing. |
| Pace | 10 actions in 10 seconds, then refused. |
| Audit | Every action is in the Logbook and in the event `lumashow_action`, and counted. |

## What it adds to Home Assistant

One device per Show (under the same device page as its ESPHome entry), with:

| Entity | What it is |
|---|---|
| *Connected* | Whether the Show is there now. |
| *Entities shared* | How many entities it is given (attribute: how many it may control). |
| *Last push* | When Home Assistant last sent it something (at most once a minute). |
| *Actions* | How many actions Home Assistant has carried out for the Show (attributes: *refused*, how many it said no to, and *last_action*). |
| *Pause control* (switch) | On: the Show may look, and nothing it asks for is done. Remembered across restarts. |
| *Message* (notify) | `notify.send_message` on it shows a message on the Show. |

## Stop it, or take its access away

| You want to | Do this | What happens |
|---|---|---|
| Stop all actions now | Turn on the Show's **Pause control** switch. | The Show can still look; nothing it asks for is done. |
| Stop sharing one thing | *Configure › Edit the list*, take it off, *Save*. | The Show drops it at once. |
| Cut the Show off | *Settings › Devices & services › LumaShow*, the three dots, **Disable** (or **Delete**). | Home Assistant tells the Show goodbye and closes; the Show clears everything it was given. Enable the entry again to reconnect. |
| Take its key away for good | On the Show: *Settings › Home Assistant › Forget Home Assistant*; then delete the Show's ESPHome device in Home Assistant. | The Show drops its key, ends the connection at once and lets nobody in until it is paired again with a **new** key. The old key opens nothing. |
| Remove LumaShow | Delete the entry, then HACS › LumaShow › the three dots › **Remove**, and restart. | Nothing of it is left running. |

If the Show is ever paired again the entry says it needs you to **Reauthenticate**: press it; the new key is read from the Show's ESPHome device.

## Alarms

Shows that share alarms keep their alarm clocks in step through one calendar. Once:

1. *Settings › Devices & services › Add integration › **Local Calendar***, named exactly **LumaShow alarms** (its entity is `calendar.lumashow_alarms`).
2. *Settings › System › General*: the **time zone** must be the Shows' (a Show in another zone syncs nothing).
3. On each Show's entry: *Configure › Notifications, cameras and alarms › **Share alarms***.

An alarm made on one Show is then on the others, and you can make or change one in Home Assistant's calendar: the title is the name, `(off) ` in front switches it off everywhere, and the first lines of the description may be `volume: 60`, `sound: dawn` and `ring: one` (only the nearest Show rings; the others wait a minute and ring only if nobody stopped it). If you and a Show change the same thing at the same time, your edit in the calendar wins. An alarm answered on a Show is also the event `lumashow_alarm` (`kind: answered`), so an automation can ring a speaker or a phone as a backup. The protocol is in [`docs/protocol.md`](docs/protocol.md) §10.

## Privacy and security

- **Local only.** No cloud, no account, no port opened to the internet. Home Assistant connects to the Show on your network.
- **Encrypted and authenticated.** The channel is the Noise protocol (`Noise_NNpsk0_25519_ChaChaPoly_SHA256`) in the framing ESPHome uses, with a key derived from the Show's ESPHome key (HMAC-SHA256), so a handshake made for one protocol never opens the other. Home Assistant keeps its own copy of the key in the entry (as ESPHome keeps its own); **diagnostics remove the key and the PIN**.
- **The integration decides, never the Show.** What the Show may see, which actions exist, whether a PIN is needed and the pace are all checked in Home Assistant. A Show that was taken over could ask for a listed action on an entity you shared, and for nothing else.
- Found a security problem? Please report it privately through the repository's *Security* tab (*Report a vulnerability*) rather than in a public issue.

## If something is wrong

- *The Show says "Not paired with Home Assistant yet"*: pair it (above).
- *The entry says it needs you to sign in again ("Reauthenticate")*: the Show's key changed. Press it.
- *Nothing happens when you tap something on the Show*: *Logbook*, look for *"<Show>: ..."* lines. A refused action says why (not shared, read-only, paused, too many at once, not one of the Show's actions, needs a PIN).
- *Diagnostics* (the entry's three dots) are safe to attach to a bug report: the key and the PIN are removed.

## Not in 0.1.0

The PIN pad and confirmation on the Show (until it has one, sensitive actions cannot be made from the Show), the media, presence and intercom modules (the protocol reserves their names), and the camera wall's use of the camera list.

## Development

```
pip install -r requirements_test.txt
pytest -q
ruff check . && ruff format --check .
```

The tests run the integration against a **fake Show** on a real local socket (`tests/fake_show.py`) and, for the maintainer, against the real Show-side code when `LUMASHOW_SHOW_BIN` names its host example (`tests/test_e2e_rust.py`). The brand images are drawn by `tools/render_brand.py`.

MIT licence. See [`CHANGELOG.md`](CHANGELOG.md).
