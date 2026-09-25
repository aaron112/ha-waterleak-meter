# Water Leak Detection for Meters

Detects water leaks from a cumulative consumption meter (`ft³`) whose readings
only move in coarse pulses. Monitors **pulse cadence**: water flowing too
continuously for too long = leak. UI-configured, sends alerts through any
`notify.*` service, survives restarts. The meter's pulse size is configurable,
so the minimum detectable leak is computed and reported rather than guessed.

Install: add this repo via **HACS → ⋯ → Custom repositories** (type
*Integration*), then *Settings → Devices & Services → Add integration → Water
Leak Detection for Meters*, pick your meter + thresholds + pulse size, done.

See [README](https://github.com/aaron112/ha-waterleak-meter) for the full picture,
tuning guidance, and the physics floor.
