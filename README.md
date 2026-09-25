# Water Leak Detector

> **Home Assistant integration** that detects water leaks from a cumulative
> consumption meter (`ft³`) that only reports in coarse pulses, using pulse
> cadence instead of flow rate.

## Why pulse cadence?

A meter that reports in ~2 ft³ increments makes instantaneous flow useless:
the reading jumps `0 -> spike -> 0 -> spike`. So this integration watches
*how often* pulses arrive:

- pulses arriving closer together than **quiet threshold** (default 45 min)
  mean "water is still flowing";
- once continuous activity outlasts the **leak threshold** (default 120 min),
  a leak is declared and you get a notification;
- when no pulse has arrived for a full quiet window, the leak state clears and
  a "resolved" notification is sent.

`unknown` readings from RF dropouts are ignored, so signal loss cannot cause
false alarms.

## Feature

- Fully configured from the UI (Integration → Add integration → Water Leak Detector)
- Entities:
  - `binary_sensor.water_leak_detected` — problem sensor (leak active)
  - `sensor.water_leak_continuous_activity` (min) — accumulated continuous flow
  - `sensor.water_leak_last_pulse` (timestamp) — last meter increment
  - `switch.water_leak_suppress_alerts` — silence overnight/garden/pool use
- Sends notifications through any `notify.*` service (e.g. `notify.telegram`),
  fallback to a persistent notification
- Fires `water_leak_detected` / `water_leak_resolved` events for your own
  automations
- State survives restarts (JSON storage)

## Install

1. Install via HACS: **HACS → ⋯ → Custom repositories →**
   `https://github.com/aaron112/ha-waterleak-meter` (type **Integration**), or copy
   `custom_components/water_leak/` into `config/custom_components/` and restart.
2. *Settings → Devices & Services → Add integration → Water Leak Detector*.
3. Pick your water meter entity, the two thresholds, and the notification
   service. Done.

## Caveats

- **Physics floor:** a 2 ft³ (~15 gal) pulse is the smallest event this meter
  can see. Leaks that never produce a pulse in a quiet window (below roughly
  1,250 L/day) are undetectable with this meter — save the *daily baseline*
  approach for those.
- A leak that happens entirely during an RF/HA outage is only seen once pulses
  resume.

## License

MIT