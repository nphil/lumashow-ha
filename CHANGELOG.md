# Changelog

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
