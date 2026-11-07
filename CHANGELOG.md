# Changelog

## 0.3.2

Ready for Home Assistant 2026.11. Tested on 2026.10 and 2026.11 (what it calls is there from 2026.9, the oldest it is written for). The tests now run against the newest stable and the beta Home Assistant every week.

- **Parts of a device (Home Assistant 2026.11).** Z-Wave, Tuya and ZHA devices are now made of a device and *child devices* (a double outlet is one device with a part for each socket). A part has no
  area of its own: it stands where its device stands. Choosing an area under *Configure* now also finds the entities of the parts of the devices in it, choosing a device takes its parts too, and the
  area a Show is told for such an entity is the one it really stands in (it was none). A part's entity is called by its device and itself ("Garage outlet Socket 1"), as Home Assistant names it.
- **Renaming an entity's id** (2026.11 changes it in place): the entities of a Show ask for that, so renaming one of them in Home Assistant no longer takes it away and brings it back.
- **Tests** run on Home Assistant's new test harness (`pytest-homeassistant`, which replaces the community package and is versioned like Home Assistant), and the weekly *HA compatibility* run on GitHub
  tests the newest stable and beta release. One test that held a date in the past (a hand-made calendar event) no longer does. The manifest says what the integration is for (`other_ecosystems`).

## 0.3.1

A lamp's colour and effects and a thermostat's limits and humidity reach a LumaTV, so that its card can draw the colour wheel, the effects and the lamp's own white range, and a thermostat's sheet.

- **Lamps** also send `hs_color`, `effect_list` and the lamp's colour-temperature limits (`min_color_temp_kelvin`, `max_color_temp_kelvin`). `effect_list` is cut at its first 128 names, each of at most
  64 characters (a floor lamp lists over a hundred); every other list over 16 entries is still dropped.
- **Thermostats** also send `min_temp`, `max_temp`, `target_temp_step`, `current_humidity` and `humidity`.
- Nothing else changes: what a device may do is still only what *Configure › What the Show may control* allows.

## 0.3.0

A weather module and a per-device way to unlock a lock from a LumaTV's own confirmation.

- **Weather** (module `weather`, protocol §10d): *Send a weather entity to the device* (off by default) lets a device ask for the list of weather entities and show one of them, chosen on the
  device: its reading and its hourly and daily forecasts (`weather.get_forecasts`), sent when chosen, when the entity changes and every 15 minutes. Nothing else about Home Assistant is sent.
- **Unlock from this device with a confirmation only** (option per device, off by default; *Configure › Notifications, cameras and alarms*): for a lock allowed under *Sensitive actions*,
  `lock.unlock` from that device is done when it carries the device's own confirmation (`origin: touch`, `confirmed: true`, as a LumaTV sends it after its on-screen question is answered with the
  remote), with no PIN. Every other origin, an unconfirmed request, `lock.open` and every other sensitive verb are refused as before, and the option holds for no other device. A lock can be allowed
  under *Sensitive actions* before a PIN is set while the option is on. Paused control and the wrong-PIN lockout stop it too; the unlock is in the Logbook and the `lumashow_action` event as `lock.unlock`, `touch`.
- Camera relay: a stopped relay's TEARDOWN is now read through to its answer before the socket closes (a socket closed with the restream's pictures still unread is reset, which could drop the request and leave go2rtc's session to time out), and the reading task is stopped first.

## 0.2.0

For a LumaTV (the LG TV shell) as one more LumaShow device: camera events and live camera streams. Both are **off for every device** until
*Send camera events* is ticked on that device's entry; a Show that does not ask for these modules is never sent them.

- **Camera events** (module `camera_events`, protocol §10b): what a shared camera's detectors see (a person, a vehicle, an animal, a package, a doorbell ring, motion) is sent to the
  device as it happens, for a pop-up on a LumaTV or a card on a Show. The detectors are the camera's own (the `binary_sensor` and `event` entities of its device, and an `event` entity
  elsewhere that names the camera); nothing of an entity the device could not already see is sent. Two sightings of one kind within 2 s are one event.
- **Camera live streams** (module `camera_live`, protocol §10c): a device with a video decoder (a LumaTV) asks for a shared camera's stream and gets it one of two ways, without ever
  seeing the camera's address or password. The **relay**: Home Assistant's own go2rtc restreams the camera on the loopback, the integration reads that restream inside Home Assistant and
  sends the pictures to the device over the paired channel (binary frames; sub-second; the cost on Home Assistant is copying packets). Else **HLS**: Home Assistant's stream integration
  serves the camera at a tokened address the device fetches (a few seconds behind). The relay needs the go2rtc Home Assistant runs itself.
- Option per device: **Send camera events** (off by default; *Configure › Notifications, cameras and alarms*), which offers both modules. Nothing runs for them while no device shares them.
- Transport: a frame type other than the message type is a binary module's (type 2 is the relay's); the device says which it takes.

## 0.1.0

The first version.

- A Show is found by itself (`_lumashow._tcp`) and added with its ESPHome device's key (nothing typed); the key is asked for only when there is no ESPHome entry.
- **Nothing is shared until you add something.** Quick starts by area, by device and by type add to one list, once; the list is edited one entity at a time. *Copy what Assist can see* is an optional helper.
- Home Assistant sends the Show only that list (and the names of the areas those entities stand in).
- Control: a fixed table of actions per kind of entity; the Show asks, Home Assistant decides. Unknown actions are refused and logged. Scripts, automations and the like only when you name them.
- Sensitive actions (unlock, disarm, open a garage, gate or door, open a valve, switch a camera or siren off) are off by default and need a PIN you set, a fresh touch and a confirmation on the Show. The Show has no PIN pad yet, so they cannot be made from it in this version.
- Every action is written in the Logbook as "<Show>: ...", counted, held to 10 in 10 seconds, and stopped at once by the *Pause control* switch.
- Entities: *Connected*, *Entities shared*, *Last push*, *Actions*, *Pause control*, and a notify entity that shows a message on the Show.
- Cameras (signed picture addresses) and Home Assistant's own notifications go to the Show only when turned on.
- Diagnostics with the key and the PIN removed. Brand images. Protocol version 1 (`docs/protocol.md`), with room for more modules.
- **Alarms** (module `alarms`, protocol §10): the alarm clocks of several Shows kept in step, with one Local Calendar (*LumaShow alarms*) as the source of truth. A Show makes, changes and deletes alarms
  (compare-and-swap on each alarm's `rev`; a person's edit in the calendar panel is a new `rev` too), every Show that shares alarms is sent the set after every change, and the Shows' beacons and answers are
  passed on to each other so that only the nearest device rings (the others a minute later, if nobody answered). An answer is also the event `lumashow_alarm`, for an automation.
- Option per Show: **Share alarms** (off by default; *Configure > Notifications, cameras and alarms*). Nothing runs for alarms while no Show shares them.
- Tested with Home Assistant 2026.9 and 2026.10, against a fake Show and against the real Show-side code.
