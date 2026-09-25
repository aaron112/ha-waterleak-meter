# Water Leak Detector

Detects water leaks from a cumulative consumption meter (`ft³`) whose readings
only move in ~2 ft³ pulses. Monitors **pulse cadence**: water flowing too
continuously for too long = leak. UI-configured, sends alerts through any
`notify.*` service, survives restarts.

Install: add this repo via **HACS → ⋯ → Custom repositories** (type
*Integration*), then *Settings → Devices & Services → Add integration → Water
Leak Detector*, pick your meter + thresholds, done.

See [README](https://github.com/aaron112/ha-waterleak-meter) for the full picture,
tuning guidance, and the physics floor.