# Water Leak Detection for Meters

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

- Fully configured from the UI (Integration → Add integration → Water Leak Detection for Meters)
- Configurable meter pulse size — the minimum detectable leak is computed from
  it and the quiet threshold, and shown on the activity sensor
- Entities:
  - `binary_sensor.water_leak_detector_leak_detected` — problem sensor (leak active)
  - `binary_sensor.water_leak_detector_meter_signal` — problem sensor (no meter data)
  - `sensor.water_leak_detector_continuous_activity` (min) — accumulated continuous flow
  - `sensor.water_leak_detector_last_pulse` (timestamp) — last meter increment
  - `switch.water_leak_detector_suppress_alerts` — silence overnight/garden/pool use
- Sends notifications through any `notify.*` service — picked from a dropdown
  of the ones installed on your system (clear it to disable; a failed send
  falls back to a persistent notification). Optionally add extra per-service
  data as JSON — e.g. `{"chat_id": "123456"}` to target a Telegram channel —
  merged into every notification call. A *Send test notification* action is
  available from the integration's options screen to verify it all works
- Fires `water_leak_detected` / `water_leak_resolved` events for your own
  automations
- Alerts you when the meter stops reporting — signal-loss timeout is
  configurable (default 3 h, `0` disables it) via
  `water_leak_signal_lost` / `water_leak_signal_restored` events and the
  *Meter signal* sensor
- State survives restarts (JSON storage)

## Install

1. Install via HACS: **HACS → ⋯ → Custom repositories →**
   `https://github.com/aaron112/ha-waterleak-meter` (type **Integration**), or copy
   `custom_components/water_leak_meter/` into `config/custom_components/` and restart.
2. *Settings → Devices & Services → Add integration → Water Leak Detection for
   Meters*.
3. Pick your water meter entity, the thresholds, the meter's pulse size, the
   signal-loss timeout, and the notification service (a dropdown of your
   installed `notify.*` services — leave it empty for no notifications). Done.

## Caveats

- **Physics floor:** the smallest leak this meter can surface is one pulse per
  quiet window — anything slower never fills a pulse, so it stays invisible to
  cadence detection. Enter your meter's pulse size during setup and the
  integration reports exactly where that floor sits (the
  `min_detectable_leak_l_day` attribute on the *Continuous activity* sensor).
  At the defaults (2 ft³ pulse, 45 min quiet) that is
  `2 ft³ × 28.3 L × 24 h / 0.75 h ≈ 1,812 L/day`. A smaller pulse size or a
  shorter quiet threshold lowers the floor; below it, only a *daily baseline*
  approach works.
- A leak that happens entirely during an RF/HA outage is only seen once pulses
  resume.

## License

MIT