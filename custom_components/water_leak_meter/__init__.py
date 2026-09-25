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
import logging
import math
import time
from typing import Any, Callable

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_LIMIT_MIN,
    CONF_NOTIFY_SERVICE,
    CONF_PULSE_FT3,
    CONF_QUIET_MIN,
    CONF_WATER_METER,
    DEFAULT_LIMIT_MIN,
    DEFAULT_PULSE_FT3,
    DEFAULT_QUIET_MIN,
    DOMAIN,
    EVENT_LEAK_DETECTED,
    EVENT_LEAK_RESOLVED,
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

        self.last_value: float | None = None
        self.last_pulse_ts: float | None = None
        self.activity: float = 0.0
        self.leak_active: bool = False
        self.suppressed: bool = False

        self._store = Store(self.hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self._timer: Callable[[], None] | None = None
        self._watchdog_seq: int = 0
        self._unsub_track: Callable[[], None] | None = None
        self._entities: list[Any] = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.last_value = data.get("last_value")
        self.last_pulse_ts = data.get("last_pulse_ts")
        self.activity = float(data.get("activity", 0.0))
        self.leak_active = bool(data.get("leak_active", False))
        _LOGGER.debug(
            "Loaded %s: value=%s, pulse=%s, activity=%.1f, leak=%s",
            self.entry.entry_id,
            self.last_value,
            self.last_pulse_ts,
            self.activity,
            self.leak_active,
        )

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "last_value": self.last_value,
                "last_pulse_ts": self.last_pulse_ts,
                "activity": self.activity,
                "leak_active": self.leak_active,
            }
        )

    def async_start_tracking(self) -> None:
        self._unsub_track = async_track_state_change_event(
            self.hass, [self.water_meter], self._on_meter_change
        )

    async def _on_meter_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or not _is_number(new_state.state):
            return
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
        self._notify_entities()

    async def _send_alert(self) -> None:
        self.hass.bus.async_fire(EVENT_LEAK_DETECTED, {"activity_min": self.activity})
        await self._notify(
            "Water leak detected",
            f"Water has been flowing nearly non-stop for over "
            f"{self.activity:.0f} minutes. Check toilets, dishwasher, washing "
            f"machine, water heater and outdoor taps!",
        )

    async def _send_resolved(self) -> None:
        self.hass.bus.async_fire(EVENT_LEAK_RESOLVED)
        await self._notify(
            "Water leak resolved",
            f"No water flow for over {self.quiet_min} minutes. "
            f"Leak assumed resolved.",
        )

    async def _notify(self, title: str, message: str) -> None:
        if not self.notify_service:
            return
        if not self.notify_service.startswith("notify."):
            _LOGGER.warning("Invalid notify service configured: %s", self.notify_service)
            return
        try:
            await asyncio.wait_for(
                self.hass.services.async_call(
                    "notify",
                    self.notify_service.split(".", 1)[1],
                    {"title": title, "message": message},
                ),
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
                notification_id=f"water_leak_{'alert' if 'detected' in title else 'resolved'}",
            )

    def set_suppressed(self, value: bool) -> None:
        self.suppressed = value
        self._notify_entities()

    @property
    def last_pulse_iso(self) -> str | None:
        if self.last_pulse_ts is None:
            return None
        return dt_util.utc_from_timestamp(self.last_pulse_ts).isoformat()

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