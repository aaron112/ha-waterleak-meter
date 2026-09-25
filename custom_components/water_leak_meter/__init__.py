"""Water Leak Detection for Meters integration.

Detects leaks from a cumulative water-consumption meter (ft³) that only
reports in coarse pulses (~2 ft³) by watching pulse cadence: pulses arriving
closer together than the "quiet" threshold keep the flow flagged as
continuous; once continuous activity outlasts the "limit" threshold a leak is
declared and a notification is sent. "unknown" readings from RF dropouts are
ignored, so they cannot cause false alarms.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LIMIT_MIN,
    CONF_NOTIFY_DATA,
    CONF_NOTIFY_SERVICE,
    CONF_PULSE_FT3,
    CONF_QUIET_MIN,
    CONF_STALE_MIN,
    CONF_WATER_METER,
    DEFAULT_LIMIT_MIN,
    DEFAULT_PULSE_FT3,
    DEFAULT_QUIET_MIN,
    DEFAULT_STALE_MIN,
    DOMAIN,
    EVENT_LEAK_DETECTED,
    EVENT_LEAK_RESOLVED,
    EVENT_SIGNAL_LOST,
    EVENT_SIGNAL_RESTORED,
    LITERS_PER_CUBIC_FOOT,
    PLATFORMS,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = WaterLeakHub(hass, entry)
    await hub.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    hub.async_start_tracking()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = hass.data[DOMAIN].pop(entry.entry_id)
    hub.async_shutdown()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _is_number(value: str | None) -> bool:
    if value is None:
        return False
    try:
        result = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(result)


def _parse_notify_data(raw: str | None) -> dict[str, Any]:
    """Parse the configured notify extra-data JSON; {} if empty/invalid."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        _LOGGER.warning("Invalid notify_data JSON, ignoring: %s", raw)
        return {}
    if not isinstance(obj, dict):
        _LOGGER.warning("notify_data must be a JSON object, ignoring: %s", raw)
        return {}
    return obj


class WaterLeakHub:
    """Holds detection state and reacts to meter pulses."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.water_meter: str = entry.options.get(CONF_WATER_METER)
        self.quiet_min: int = int(entry.options.get(CONF_QUIET_MIN, DEFAULT_QUIET_MIN))
        self.limit_min: int = int(entry.options.get(CONF_LIMIT_MIN, DEFAULT_LIMIT_MIN))
        self.pulse_ft3: float = float(
            entry.options.get(CONF_PULSE_FT3, DEFAULT_PULSE_FT3)
        )
        self.notify_service: str = (
            entry.options.get(CONF_NOTIFY_SERVICE) or ""
        ).strip()
        self.stale_min: int = int(
            entry.options.get(CONF_STALE_MIN, DEFAULT_STALE_MIN)
        )
        self.notify_data: dict[str, Any] = _parse_notify_data(
            entry.options.get(CONF_NOTIFY_DATA, "")
        )

        self.last_value: float | None = None
        self.last_pulse_ts: float | None = None
        self.activity: float = 0.0
        self.leak_active: bool = False
        self.suppressed: bool = False
        self.signal_lost: bool = False
        self.unavailable_since: float | None = None

        self._store = Store(self.hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self._timer: Callable[[], None] | None = None
        self._stale_timer: Callable[[], None] | None = None
        self._watchdog_seq: int = 0
        self._stale_seq: int = 0
        self._unsub_track: Callable[[], None] | None = None
        self._entities: list[Any] = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.last_value = data.get("last_value")
        self.last_pulse_ts = data.get("last_pulse_ts")
        self.activity = float(data.get("activity", 0.0))
        self.leak_active = bool(data.get("leak_active", False))
        self.signal_lost = bool(data.get("signal_lost", False))
        self.unavailable_since = data.get("unavailable_since")
        _LOGGER.debug(
            "Loaded %s: value=%s, pulse=%s, activity=%.1f, leak=%s, signal_lost=%s",
            self.entry.entry_id,
            self.last_value,
            self.last_pulse_ts,
            self.activity,
            self.leak_active,
            self.signal_lost,
        )
        self._check_initial_state()
        if self.leak_active:
            self._schedule_watchdog()
        if (
            self.last_value is not None
            and self.last_pulse_ts is not None
            and not self.leak_active
            and self.activity > 0.0
            and (time.time() - self.last_pulse_ts) / 60.0 >= self.quiet_min
        ):
            # The meter has been quiet since its last pulse: stale accumulated
            # activity must not resurrect phantom sensor minutes after a
            # restart. Persist the reset so it does not come back on the next
            # boot either.
            self.activity = 0.0
            await self._save()

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "last_value": self.last_value,
                "last_pulse_ts": self.last_pulse_ts,
                "activity": self.activity,
                "leak_active": self.leak_active,
                "signal_lost": self.signal_lost,
                "unavailable_since": self.unavailable_since,
            }
        )

    def async_start_tracking(self) -> None:
        self._unsub_track = async_track_state_change_event(
            self.hass, [self.water_meter], self._on_meter_change
        )

    def _check_initial_state(self) -> None:
        """Recover or re-arm signal-loss state based on the meter right now."""
        state = self.hass.states.get(self.water_meter)
        if state is None:
            return
        if not _is_number(state.state):
            if self.unavailable_since is None:
                self._on_unavailable(time.time())
            else:
                # Mid-outage restart: keep the original clock start and re-arm
                # the stale check for the remaining time, so a still-dead meter
                # still trips EVENT_SIGNAL_LOST at the real stale deadline.
                self._schedule_stale_check(
                    max(
                        0.0,
                        self.stale_min * 60 - (time.time() - self.unavailable_since),
                    )
                )
        elif self.signal_lost:
            asyncio.create_task(self._on_available())

    async def _on_meter_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if not _is_number(new_state.state):
            self._on_unavailable(time.time())
            return
        await self._on_available()
        value = float(new_state.state)
        if self.last_value is not None and value <= self.last_value:
            return

        now = time.time()
        gap_min = (
            9999.0 if self.last_pulse_ts is None else (now - self.last_pulse_ts) / 60.0
        )
        if gap_min < self.quiet_min:
            self.activity += gap_min
        else:
            self.activity = 0.0

        self.last_value = value
        self.last_pulse_ts = now
        self._schedule_watchdog()
        await self._save()
        self._notify_entities()

        if (
            not self.suppressed
            and not self.leak_active
            and self.activity >= self.limit_min
        ):
            self.leak_active = True
            await self._save()
            await self._send_alert()

    @callback
    def _schedule_watchdog(self) -> None:
        if self._timer:
            self._timer()
        self._watchdog_seq += 1
        seq = self._watchdog_seq

        async def _watchdog(_now: Any, token: int = seq) -> None:
            await self._on_quiet(token)

        self._timer = async_call_later(self.hass, self.quiet_min * 60, _watchdog)

    async def _on_quiet(self, token: int) -> None:
        # A stale firing (already-queued timer that raced a new pulse) must
        # stand down so it cannot clear a freshly scheduled watchdog.
        if token != self._watchdog_seq:
            return
        self._timer = None
        was_leak = self.leak_active
        had_activity = self.activity > 0.0
        self.activity = 0.0
        if was_leak:
            self.leak_active = False
            await self._save()
            # A pulse landed while we were awaiting: flow resumed, alert
            # already re-fired. Don't follow it with a spurious "resolved".
            if token != self._watchdog_seq:
                self._notify_entities()
                return
            await self._send_resolved()
        elif had_activity:
            # Persist the quiet reset so stale activity cannot resurrect
            # phantom minutes if HA restarts before the next pulse.
            await self._save()
        self._notify_entities()

    def _on_unavailable(self, now: float) -> None:
        """Start (once) the clock for the current offline stretch."""
        if self.unavailable_since is None:
            self.unavailable_since = now
            self._schedule_stale_check()

    def _schedule_stale_check(self, delay: float | None = None) -> None:
        if self._stale_timer:
            self._stale_timer()
        if self.stale_min <= 0:
            return
        if delay is None:
            delay = self.stale_min * 60
        else:
            delay = max(0.0, delay)
        self._stale_seq += 1
        seq = self._stale_seq

        async def _check(_now: Any, token: int = seq) -> None:
            await self._on_signal_lost(token)

        self._stale_timer = async_call_later(self.hass, delay, _check)

    def _cancel_stale_timer(self) -> None:
        if self._stale_timer:
            self._stale_timer()
            self._stale_timer = None

    async def _on_available(self) -> None:
        was_lost = self.signal_lost
        self.unavailable_since = None
        self._cancel_stale_timer()
        if was_lost:
            self.signal_lost = False
            await self._save()
            await self._send_signal_restored()
            self._notify_entities()

    async def _on_signal_lost(self, token: int) -> None:
        if token != self._stale_seq:
            return
        self._stale_timer = None
        if self.stale_min <= 0 or self.signal_lost or self.unavailable_since is None:
            return
        elapsed = time.time() - self.unavailable_since
        if elapsed / 60.0 < self.stale_min:
            # Fired early (clock jumped, or a boot re-arm): reschedule for the
            # tail so the alarm still lands at the real stale deadline instead
            # of standing down forever.
            self._schedule_stale_check(self.stale_min * 60 - elapsed)
            return
        self.signal_lost = True
        await self._save()
        await self._send_signal_lost()
        self._notify_entities()

    async def _send_alert(self) -> None:
        self.hass.bus.async_fire(EVENT_LEAK_DETECTED, {"activity_min": self.activity})
        await self._notify(
            "Water leak detected",
            f"Water has been flowing nearly non-stop for over "
            f"{self.activity:.0f} minutes. Check toilets, dishwasher, washing "
            f"machine, water heater and outdoor taps!",
            "water_leak_alert",
        )

    async def _send_resolved(self) -> None:
        self.hass.bus.async_fire(EVENT_LEAK_RESOLVED)
        await self._notify(
            "Water leak resolved",
            f"No water flow for over {self.quiet_min} minutes. "
            f"Leak assumed resolved.",
            "water_leak_resolved",
        )

    async def _simulate_leak(self, gap_min: float | None = None) -> bool:
        """Fire a leak alert on demand, deterministically.

        Sets the continuous-flow activity to just past the leak threshold,
        spaced at a gap comfortably inside `quiet_min`, then runs the real
        detection side effects: the Leak Detected event, the alert, and the
        quiet-watchdog that resolves the leak on its own after `quiet_min` of
        quiet. Runs as a fresh flow episode and never touches the meter's own
        cumulative value or last-pulse time; failures to persist or notify are
        logged, not fatal.
        Returns True if a leak fired; False if already leaking or suppressed.
        """
        if self.leak_active or self.suppressed:
            return False
        if gap_min is None:
            gap_min = self.quiet_min / 2.0
        if gap_min <= 0 or self.limit_min <= 0:
            return False
        self.activity = gap_min * math.ceil(self.limit_min / gap_min)
        if self.activity < self.limit_min:
            self.activity = float(self.limit_min)
        self.leak_active = True
        self._schedule_watchdog()
        self._notify_entities()
        try:
            await self._save()
        except Exception:
            _LOGGER.exception("Simulated leak: could not save state")
        try:
            await self._send_alert()
        except Exception:
            _LOGGER.exception("Simulated leak: could not send alert")
        return True

    async def _send_signal_lost(self) -> None:
        self.hass.bus.async_fire(
            EVENT_SIGNAL_LOST,
            {"water_meter": self.water_meter, "stale_min": self.stale_min},
        )
        await self._notify(
            "Water meter signal lost",
            f"No reading from {self.water_meter} for over {self.stale_min} "
            f"minutes. Check the bridge/receiver power and range, then reset "
            f"the integration if it stays down.",
            "water_leak_signal_lost",
        )

    async def _send_signal_restored(self) -> None:
        self.hass.bus.async_fire(EVENT_SIGNAL_RESTORED)
        await self._notify(
            "Water meter signal restored",
            f"{self.water_meter} is reporting again.",
            "water_leak_signal_restored",
        )

    async def _notify(self, title: str, message: str, notification_id: str) -> None:
        if not self.notify_service:
            return
        domain, _, service = self.notify_service.partition(".")
        if domain not in ("notify", "telegram_bot"):
            _LOGGER.warning("Invalid notify service configured: %s", self.notify_service)
            return
        call_data: dict[str, Any] = {"title": title, "message": message}
        if self.notify_data:
            if domain == "notify":
                call_data["data"] = dict(self.notify_data)
            else:
                call_data.update(self.notify_data)
        try:
            await asyncio.wait_for(
                self.hass.services.async_call(domain, service, call_data),
                timeout=10,
            )
        except Exception as err:
            _LOGGER.warning(
                "%s failed (%s); falling back to persistent notification",
                self.notify_service,
                err,
            )
            persistent_notification.async_create(
                self.hass,
                message,
                title,
                notification_id=notification_id,
            )

    def set_suppressed(self, value: bool) -> None:
        self.suppressed = value
        self._notify_entities()

    @property
    def last_pulse_dt(self) -> datetime | None:
        """Timezone-aware last-pulse timestamp; the TIMESTAMP sensor needs a datetime."""
        if self.last_pulse_ts is None:
            return None
        return dt_util.utc_from_timestamp(self.last_pulse_ts)

    @property
    def min_detectable_leak_l_day(self) -> float:
        """Smallest continuous leak this meter can surface, given pulse size."""
        return round(
            self.pulse_ft3 * LITERS_PER_CUBIC_FOOT * 24 * 60 / self.quiet_min, 1
        )

    def add_entity(self, entity: Any) -> None:
        self._entities.append(entity)
        entity.update_from_hub()
        entity.async_write_ha_state()

    def _notify_entities(self) -> None:
        for entity in self._entities:
            entity.update_from_hub()
            entity.async_write_ha_state()

    def async_shutdown(self) -> None:
        if self._unsub_track is not None:
            self._unsub_track()
        if self._timer is not None:
            self._timer()
        if self._stale_timer is not None:
            self._stale_timer()