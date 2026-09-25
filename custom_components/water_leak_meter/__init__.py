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
    LIMIT_MIN_MAX,
    LIMIT_MIN_MIN,
    LITERS_PER_CUBIC_FOOT,
    PLATFORMS,
    PULSE_FT3_MAX,
    PULSE_FT3_MIN,
    QUIET_MIN_MAX,
    QUIET_MIN_MIN,
    STALE_MIN_MAX,
    STALE_MIN_MIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not _to_text(entry.options.get(CONF_WATER_METER)):
        _LOGGER.error("No water meter configured for %s; cannot set up", entry.entry_id)
        return False
    hub = WaterLeakHub(hass, entry)
    # Load first, then track: _check_initial_state reconciles the loaded state
    # against the meter's live value, and a callback firing mid-load would have
    # its updates overwritten by the load's own assignments.
    await hub.async_load()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub
    hub.async_start_tracking()
    # Re-reconcile now that events are subscribed: the meter may have changed
    # state during the loads above, and those events were not yet observed.
    await hub._check_initial_state()
    await hub._reanchor_after_restart()
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Never leave a live hub behind a failed setup: it would keep tracking
        # and firing, and a retry would overwrite it with a second hub.
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hub.async_shutdown()
        raise
    # Registered only once setup succeeded, so a retry cannot accumulate
    # listeners on an entry that never loaded.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hub = hass.data[DOMAIN].pop(entry.entry_id)
        hub.async_shutdown()
    return unloaded


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


def _to_int(value: Any, default: int) -> int:
    """Coerce a stored option to int; any malformed value falls back to default.

    Options normally come from the config flow, but a hand-edited or legacy
    storage file can carry Os, strings, or nonsense that must not crash the
    entry load. Non-finite floats (inf/nan) are rejected too: rounding them
    raises OverflowError, and they are meaningless as a threshold.
    """
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(result):
        return default
    return int(round(result))


def _to_float(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(result):
        return default
    return result


def _to_text(value: Any) -> str:
    """Coerce an option to a stripped string; non-strings become empty."""
    return value.strip() if isinstance(value, str) else ""


def _clamp(value: int | float, low: int | float, high: int | float) -> int | float:
    """Constrain a recovered option to the range its config-flow selector allows.

    A legacy or hand-edited value outside the selector's bounds would be
    rejected by voluptuous on submit (defaults are validated like any other
    input), leaving the user unable to save the form, and an unbounded delay
    can overflow the event loop's timer.
    """
    return min(max(value, low), high)


def _parse_notify_data(raw: str | None) -> dict[str, Any]:
    """Parse the configured notify extra-data JSON; {} if empty/invalid."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        # Never log the raw text: notify_data can carry tokens or webhook
        # credentials, and a malformed paste would leak them into the log.
        _LOGGER.warning(
            "Invalid notify_data JSON (%d chars), ignoring", len(raw)
        )
        return {}
    if not isinstance(obj, dict):
        _LOGGER.warning(
            "notify_data must be a JSON object (%d chars), ignoring", len(raw)
        )
        return {}
    return obj


class WaterLeakHub:
    """Holds detection state and reacts to meter pulses."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.water_meter: str = _to_text(entry.options.get(CONF_WATER_METER))
        # Clamp to the same bounds the config flow's selectors enforce: an
        # absurd legacy value must not reach async_call_later, where the
        # resulting delay can overflow and break every timer.
        self.quiet_min: int = _clamp(
            _to_int(entry.options.get(CONF_QUIET_MIN), DEFAULT_QUIET_MIN),
            QUIET_MIN_MIN,
            QUIET_MIN_MAX,
        )
        self.limit_min: int = _clamp(
            _to_int(entry.options.get(CONF_LIMIT_MIN), DEFAULT_LIMIT_MIN),
            LIMIT_MIN_MIN,
            LIMIT_MIN_MAX,
        )
        self.pulse_ft3: float = _clamp(
            _to_float(entry.options.get(CONF_PULSE_FT3), DEFAULT_PULSE_FT3),
            PULSE_FT3_MIN,
            PULSE_FT3_MAX,
        )
        self.notify_service: str = _to_text(entry.options.get(CONF_NOTIFY_SERVICE))
        self.stale_min: int = _clamp(
            _to_int(entry.options.get(CONF_STALE_MIN), DEFAULT_STALE_MIN),
            STALE_MIN_MIN,
            STALE_MIN_MAX,
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
        self._pulse_seq: int = 0
        self._cadence_reanchor_pending: bool = False
        self._stopped: bool = False
        self._unsub_track: Callable[[], None] | None = None
        self._entities: list[Any] = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        if not isinstance(data, dict):
            _LOGGER.warning(
                "Ignoring malformed stored state for %s: not a mapping", self.entry.entry_id
            )
            data = {}
        last_value = data.get("last_value")
        self.last_value = _to_float(last_value, 0.0) if last_value is not None else None
        last_pulse = data.get("last_pulse_ts")
        self.last_pulse_ts = (
            _to_float(last_pulse, 0.0) if last_pulse is not None else None
        )
        if self.last_pulse_ts is not None and self.last_pulse_ts > time.time():
            # A pulse from the future is corrupt; a negative gap would
            # otherwise subtract from the accumulated evidence.
            _LOGGER.warning(
                "Stored last_pulse_ts for %s is in the future; discarding it",
                self.entry.entry_id,
            )
            self.last_pulse_ts = None
        self.activity = _to_float(data.get("activity"), 0.0)
        self.leak_active = data.get("leak_active") is True
        self.signal_lost = data.get("signal_lost") is True
        unavailable_since = data.get("unavailable_since")
        self.unavailable_since = (
            _to_float(unavailable_since, 0.0)
            if unavailable_since is not None
            else None
        )
        if self.unavailable_since is not None and self.unavailable_since > time.time():
            # A clock from the future is corrupt. Keeping it would make every
            # stale check reschedule for another full window, forever.
            _LOGGER.warning(
                "Stored outage clock for %s is in the future; restarting it",
                self.entry.entry_id,
            )
            self.unavailable_since = time.time()
        if (
            self.last_value is not None
            and self.last_pulse_ts is not None
            and not self.leak_active
            and self.activity > 0.0
            and (time.time() - self.last_pulse_ts) / 60.0 >= self.quiet_min
        ):
            # Quiet since the last pulse: stale accumulated activity must not
            # resurrect phantom sensor minutes after a restart. This is only
            # correct when nothing was missed, so a changed live reading is
            # checked first below and skips this reset.
            stale = True
        else:
            stale = False
        stored_meter = data.get("water_meter")
        if stored_meter is not None and stored_meter != self.water_meter:
            # The configured meter was swapped in the options flow. The stored
            # reading/activity belonged to the old meter; carrying them over
            # would attribute the old meter's cadence to the new one and can
            # fire a false leak.
            _LOGGER.warning(
                "Configured meter changed from %s to %s; discarding stored "
                "detection state",
                stored_meter,
                self.water_meter,
            )
            self.last_value = None
            self.last_pulse_ts = None
            self.activity = 0.0
            self.leak_active = False
            self.signal_lost = False
            self.unavailable_since = None
            # Persist the discard, or the store still holds the old meter's
            # baseline and switching back later would resurrect it.
            await self._save()
        elif stored_meter is None and self.last_pulse_ts is not None:
            # Legacy state with no attribution: the configured meter may have
            # been swapped while this state was stored, so the interval since
            # that pulse is undatable. Re-anchor the clock only.
            #
            # last_value is deliberately left as stored so
            # _reanchor_after_restart can still compare the live reading against
            # the stored baseline: a changed value proves a pulse landed during
            # the downtime (keep the evidence), and a counter reset must not
            # inherit it. Overwriting it here would blind both of its guards.
            self.last_pulse_ts = time.time()
            await self._save()
        _LOGGER.debug(
            "Loaded %s: value=%s, pulse=%s, activity=%.1f, leak=%s, signal_lost=%s",
            self.entry.entry_id,
            self.last_value,
            self.last_pulse_ts,
            self.activity,
            self.leak_active,
            self.signal_lost,
        )
        await self._check_initial_state()
        reanchored = await self._reanchor_after_restart()
        if stale and not reanchored:
            # Nothing was missed while we were down and the meter has been
            # quiet well past the threshold: the accumulated activity is stale
            # and must not resurrect phantom sensor minutes. Persist the reset
            # so it does not come back on the next boot either.
            self.activity = 0.0
            await self._save()
        if self.hass.states.get(self.water_meter) is None:
            # The meter entity does not exist (yet). _check_initial_state has
            # started an outage for it, so the leak stands — silence from a
            # meter that is not there proves nothing. When the entity appears,
            # the recovery path arms the quiet window, and an unchanged
            # reading then resolves the leak on real quiet time.
            return
        if self.leak_active:
            # A persisted leak already served part of its quiet window before
            # the restart; resume the remainder so a leak cannot stay active
            # forever across frequent restarts.
            self._schedule_watchdog(
                None
                if self.last_pulse_ts is None
                else max(0.0, self.quiet_min * 60 - (time.time() - self.last_pulse_ts))
            )
        elif self.activity > 0.0:
            # Preserved pre-restart evidence needs a watchdog too, or it sits
            # stale in a running instance until an unrelated pulse arrives.
            self._schedule_watchdog()

    async def _reanchor_after_restart(self) -> bool:
        """Re-anchor cadence to the live reading; downtime is not evidence.

        While Home Assistant was down the meter kept (or stopped) reporting
        unseen. Charging that blind interval as continuous flow manufactures
        leaks, so the stored pulse clock is only trusted when the meter's
        current value still matches the stored baseline — meaning nothing was
        missed. Any other case (a new value, or a numeric value with no
        baseline at all) is treated as a fresh reading.

        Returns True when the stored pulse clock was replaced, i.e. the
        downtime was undatable or a pulse landed while we were down.
        """
        state = self.hass.states.get(self.water_meter)
        if state is None or not _is_number(state.state):
            return False
        current = float(state.state)
        # A cumulative water meter cannot read zero or below, so the reset
        # heuristics below (ratios) are only meaningful above that. A
        # non-positive reading is re-anchored like any other, never treated as
        # a counter reset — otherwise a meter reporting negatives would be
        # permanently reset and detection would never accumulate again.
        if (
            self.last_value is not None
            and self.last_value > 0
            and current > 0
            and current < self.last_value * 0.5
        ):
            # A far lower reading is a counter reset or a swapped meter, not a            # pulse. Restart from scratch exactly as a live drop would, or the
            # old evidence is attributed to an unrelated counter.
            _LOGGER.warning(
                "Meter %s restarted from %s to %s while Home Assistant was "
                "down; discarding stored detection state",
                self.water_meter,
                self.last_value,
                current,
            )
            self.last_value = current
            self.last_pulse_ts = time.time()
            self.activity = 0.0
            was_leak = self.leak_active
            self.leak_active = False
            await self._save()
            if was_leak and not self._stopped:
                await self._send_resolved(
                    f"♻️ The counter on {self.meter_display_name} was "
                    "reset or the meter was replaced; the old flow data was "
                    "discarded."
                )
            return True
        if self.leak_active:
            if self.last_value is not None and current != self.last_value:
                # A pulse landed while HA was down, so flow is ongoing: the
                # quiet window restarts from now. Resuming the old deadline
                # would resolve the leak while water is still moving.
                self.last_pulse_ts = time.time()
                self.last_value = current
                await self._save()
                return True
            # Otherwise the window is measured from the stored pulse and the
            # caller resumes its remainder, so a leak cannot stay active
            # forever across frequent restarts.
            return False
        if (
            self.last_value is not None
            and current == self.last_value
            and self.last_pulse_ts is not None
        ):
            # Nothing was missed while we were down; the gap is real.
            return False
        # The stored pulse clock is replaced, but the accumulated activity is
        # real evidence observed before the restart and is kept: only the
        # undatable interval is dropped. A near-threshold episode must not be
        # forgotten just because HA bounced.
        self.last_value = current
        self.last_pulse_ts = time.time()
        await self._save()
        return True

    async def _save(self) -> None:
        await self._store.async_save(
            {
                "water_meter": self.water_meter,
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

    async def _check_initial_state(self) -> None:
        """Recover or re-arm signal-loss state based on the meter right now."""
        state = self.hass.states.get(self.water_meter)
        if state is None:
            if self.unavailable_since is not None:
                # The entity does not exist (yet) but an outage is on record.
                # Re-arm from the stored clock, or the outage silently loses
                # its timer: the next unavailable event would find the clock
                # already set and do nothing.
                self._schedule_stale_check(
                    max(
                        0.0,
                        self.stale_min * 60 - (time.time() - self.unavailable_since),
                    )
                )
            return
        if not _is_number(state.state):
            if self.unavailable_since is None:
                await self._on_unavailable(time.time())
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
        elif self.signal_lost or self.unavailable_since is not None:
            # The meter is reporting now, so any persisted outage clock is
            # over: clear it. Skipping this for a non-signal_lost outage left
            # the clock set, and the next _on_unavailable would then bail out
            # (clock already "running") and never arm a stale check at all.
            await self._on_available()

    async def _on_meter_change(self, event: Event) -> None:
        if self._stopped:
            return
        # HA dispatches state callbacks as independent tasks, so two can be
        # inside this coroutine at once. _pulse_seq counts committed baselines:
        # a callback that abandons (an outage, a repeat reading) must not bump
        # it, or it would strand a real threshold-crossing pulse that is merely
        # waiting on a save.
        observed_seq = self._pulse_seq
        new_state = event.data.get("new_state")
        if new_state is None:
            # HA emits new_state=None when the tracked entity is removed from
            # the registry. That is an outage like any other: without starting
            # the clock, no signal-loss timer is armed and the quiet watchdog
            # can "resolve" a real leak for a meter that no longer exists.
            await self._on_unavailable(time.time())
            return
        if not _is_number(new_state.state):
            await self._on_unavailable(time.time())
            return
        was_recovering = self.unavailable_since is not None
        await self._on_available()
        # A concurrent callback can have consumed this pulse's re-anchor while
        # we were suspended. The latch is set before _on_available's first
        # await, so whoever reaches the cadence math next still sees that the
        # blackout span is undatable instead of charging it as flow.
        recovered = was_recovering or self._cadence_reanchor_pending
        if self._pulse_seq != observed_seq or self._stopped:
            return
        value = float(new_state.state)
        if recovered:
            # The meter is reporting again after an outage. Re-anchor the
            # cadence clock to this reading, whatever the value turns out to
            # be: a same-value or dipped recovery returns early below, and a
            # stale last_pulse_ts would otherwise let the blackout itself be
            # charged as flow on the next real increment.
            self._cadence_reanchor_pending = False
            self.last_pulse_ts = time.time()
            self._pulse_seq += 1
            self._schedule_watchdog()
        if self.last_value is not None:
            if value == self.last_value:
                # An unchanged reading is proof that no water has been consumed,
                # so the quiet window runs from it. Without this a boot that
                # found an idle meter (nothing to re-anchor) would leave any
                # persisted leak standing forever, with no timer to resolve it.
                self._schedule_watchdog()
                if recovered:
                    await self._save()
                    self._notify_entities()
                return
            if value < self.last_value:
                # A cumulative meter never decreases, so a lower read means the
                # counter was reset or replaced. Either way the old baseline is
                # unusable and must be re-anchored, or every later reading is
                # discarded forever and detection stays dead. A small dip is
                # most likely reading noise: re-anchor the value but keep the
                # pulse clock and accumulated activity so a jitter glitch
                # cannot cancel a real, nearly-fired leak. A dramatic drop is
                # a genuine new counter (meter swapped/reset): restart
                # detection from scratch because cadence across the swap is
                # meaningless.
                previous = self.last_value
                if previous <= 0 or value <= 0 or value * 2 >= previous:
                    # A meter at or below zero has no usable ratio, so fall
                    # back to the conservative re-anchor: keep the pulse clock
                    # and activity, move the baseline, stay detectable.
                    self.last_value = value
                    self._pulse_seq += 1
                    pulse_seq = self._pulse_seq
                    _LOGGER.warning(
                        "Meter %s readback dipped from %s to %s — re-anchoring "
                        "baseline, continuing to track",
                        self.water_meter,
                        previous,
                        value,
                    )
                    await self._save()
                    if self._pulse_seq != pulse_seq or self._stopped:
                        return
                    self._notify_entities()
                    return
                _LOGGER.warning(
                    "Meter %s readback dropped from %s to %s — assuming counter "
                    "reset and re-baselining",
                    self.water_meter,
                    previous,
                    value,
                )
                now = time.time()
                self.last_value = value
                self.last_pulse_ts = now
                self._pulse_seq += 1
                pulse_seq = self._pulse_seq
                was_leak = self.leak_active
                self.activity = 0.0
                # Restart detection from scratch: the old evidence (including
                # an active leak flag) belongs to the replaced counter, and
                # letting it linger strands "Leak detected" ON while "Continuous
                # activity" reads 0 until the next global quiet spell.
                self.leak_active = False
                self._schedule_watchdog()
                await self._save()
                if was_leak and not self._stopped:
                    # Announce before the generation check: the reset has
                    # already retracted the leak, and a newer reading landing
                    # mid-save must not swallow the resolution.
                    await self._send_resolved(
                        f"♻️ The counter on {self.meter_display_name} was "
                        "reset or the meter was replaced; the old flow data "
                        "was discarded."
                    )
                if self._pulse_seq != pulse_seq or self._stopped:
                    return
                self._notify_entities()
                return

        now = time.time()
        if self.last_pulse_ts is None:
            gap_min = 9999.0
        elif recovered:
            # The outage span is unknown, not evidence of continuous flow:
            # counting it would let a short RF dropout manufacture a false
            # leak. It is also not evidence of a break, so the activity
            # observed before the outage is kept and the gap contributes
            # nothing.
            gap_min = 0.0
        else:
            # A clock that moved backwards (NTP correction, corrupt store) must
            # not subtract from the accumulated evidence.
            gap_min = max(0.0, (now - self.last_pulse_ts) / 60.0)
        if gap_min < self.quiet_min:
            self.activity += gap_min
        else:
            self.activity = 0.0

        self.last_value = value
        self.last_pulse_ts = now
        self._pulse_seq += 1
        self._schedule_watchdog()
        await self._save()
        if self._stopped:
            return
        self._notify_entities()
        # Deliberately runs even when a newer callback already committed a
        # baseline: this callback's activity is real evidence, and skipping the
        # check would discard a genuine threshold crossing.
        await self._maybe_fire_leak()

    async def _maybe_fire_leak(self) -> None:
        """Declare a leak once the accumulated flow crosses the threshold.

        Separate from the pulse path so a callback superseded mid-save still
        gets the threshold evaluated: its activity is committed, and dropping
        the check would silently discard a real crossing.
        """
        if (
            not self.suppressed
            and not self.leak_active
            and self.activity >= self.limit_min
        ):
            self.leak_active = True
            await self._save()
            if not self.leak_active or self.suppressed or self._stopped:
                # While the save was awaited, a concurrent event could have
                # retracted this leak (counter reset), turned on Suppress
                # Alerts, or unloaded the entry. Firing now would announce a
                # leak the current state no longer warrants.
                return
            # Entities were pushed before the flip; push again so the
            # "Leak detected" sensor reflects the new state immediately, and
            # before the (slow) notification so a blocked send cannot leave the
            # sensor stale. If this pulse is the last one before flow stops,
            # the watchdog's later "resolved" write would otherwise be the ONLY
            # write and the sensor would never show ON for the whole episode.
            self._notify_entities()
            await self._send_alert()

    @callback
    def _schedule_watchdog(self, delay: float | None = None) -> None:
        """Arm the quiet watchdog. `delay` defaults to a full quiet window.

        Callers that resume part of an elapsed window pass the remainder
        themselves, already clamped to a non-negative, representable value.
        """
        if self._timer:
            self._timer()
        if self._stopped:
            # A callback that resumed after unload must not leave a live timer.
            return
        self._watchdog_seq += 1
        seq = self._watchdog_seq
        if delay is None:
            delay = self.quiet_min * 60

        async def _watchdog(_now: Any, token: int = seq) -> None:
            await self._on_quiet(token)

        self._timer = async_call_later(self.hass, delay, _watchdog)

    async def _on_quiet(self, token: int) -> None:
        # A stale firing (already-queued timer that raced a new pulse) must
        # stand down so it cannot clear a freshly scheduled watchdog.
        if token != self._watchdog_seq or self._stopped:
            return
        self._timer = None
        if self.unavailable_since is not None:
            # The meter went dark before the quiet window elapsed. Silence
            # proves nothing while there is no signal, so the leak stands: it
            # must not be "resolved" on evidence we never received, and it
            # must not silently stop being monitored. The watchdog is re-armed
            # when the meter reports again.
            self._schedule_watchdog()
            return
        was_leak = self.leak_active
        had_activity = self.activity > 0.0
        previous_activity = self.activity
        self.activity = 0.0
        if was_leak:
            self.leak_active = False
            await self._save()
            if self._stopped:
                return
            if self.unavailable_since is not None:
                # The meter went dark while the save was awaited. Silence
                # proves nothing without a signal, so stand this resolve down
                # and let the next quiet check (or recovery) decide.
                self.leak_active = True
                await self._save()
                self._schedule_watchdog()
                return
            if token != self._watchdog_seq:
                # A pulse landed mid-save and re-armed the watchdog without
                # crossing the leak threshold again. The quiet window is over
                # (our token fired), but the resolve-save we just wrote
                # clobbered that pulse's activity. Re-save so the fresh
                # reading's accumulation is not lost.
                #
                # This is also the only stale check needed: anything that can
                # set leak_active back to True (a crossing pulse, a simulate,
                # a counter-reset re-baseline) also re-arms the watchdog, so it
                # is always detectable as a sequence bump here.
                await self._save()
                if self._stopped or self.leak_active:
                    # Another pulse re-armed the leak while that save was
                    # awaited; it owns the state now, so this resolve is stale.
                    self._notify_entities()
                    return
                if self.unavailable_since is not None:
                    # The meter went dark during the corrective save; silence
                    # without a signal proves nothing, so stand down again.
                    self.leak_active = True
                    await self._save()
                    self._schedule_watchdog()
                    return
            await self._send_resolved()
        elif had_activity:
            # Persist the quiet reset so stale activity cannot resurrect
            # phantom minutes if HA restarts before the next pulse.
            await self._save()
            if self.unavailable_since is not None:
                # The meter went dark while this save was awaited. Silence
                # without a signal proves nothing, so the pre-outage evidence
                # stands and stays monitored.
                self.activity = previous_activity
                await self._save()
                self._schedule_watchdog()
        self._notify_entities()

    async def _on_unavailable(self, now: float) -> None:
        """Start (once) the clock for the current offline stretch.

        Similarly to mid-outage boots, an outage that starts under a running
        integration must persist its start time: if HA restarts during the
        outage, boot re-arms the stale check from the stored clock instead of
        starting a fresh one and delaying the signal-loss alarm by stale_min.
        """
        if self._stopped:
            return
        if self.unavailable_since is None:
            self.unavailable_since = now
            self._schedule_stale_check()
            await self._save()

    def _schedule_stale_check(self, delay: float | None = None) -> None:
        """Arm the signal-loss check. `delay` defaults to a full stale window.

        Callers resuming part of an elapsed window pass the remainder already
        clamped to non-negative, so the event loop never sees an
        unrepresentable delay.
        """
        if self._stale_timer:
            self._stale_timer()
        if self.stale_min <= 0:
            return
        if delay is None:
            delay = self.stale_min * 60
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
        if self._stopped:
            return
        had_clock = self.unavailable_since is not None
        was_lost = self.signal_lost
        self.unavailable_since = None
        if had_clock:
            # Latched before the first await below, so a concurrent pulse that
            # races this recovery still knows the blackout span must not be
            # charged as flow.
            self._cadence_reanchor_pending = True
        self._cancel_stale_timer()
        if was_lost:
            self.signal_lost = False
            await self._save()
            if self.signal_lost or self.unavailable_since is not None or self._stopped:
                # The meter went dark again while the save was awaited: the
                # restore is stale, so do not announce recovery for a meter
                # that is currently unavailable.
                return
            await self._send_signal_restored()
            self._notify_entities()
        elif had_clock:
            # A dropout that recovered before the stale deadline must also
            # clear the clock in the store. If HA restarts before the next
            # pulse save, boot would otherwise reload the old start time and
            # treat the recovered outage as ongoing: re-arming the stale check
            # against a bogus deadline (or refusing to clock a new outage).
            await self._save()

    async def _on_signal_lost(self, token: int) -> None:
        if token != self._stale_seq or self._stopped:
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
        if not self.signal_lost or self.unavailable_since is None or self._stopped:
            # The meter came back while the save was awaited: _on_available
            # already cleared the flag and announced the restore, so this
            # stale callback must not also announce a loss.
            return
        await self._send_signal_lost()
        self._notify_entities()

    @property
    def meter_display_name(self) -> str:
        """The meter's friendly name, e.g. "Basement Water Meter".

        Alerts read far better with a name than with `sensor.water_meter`, and
        the entity id is only a fallback for when the state is not loaded.
        """
        state = self.hass.states.get(self.water_meter)
        name = getattr(state, "name", None) if state is not None else None
        return name or self.water_meter

    async def _send_alert(self) -> None:
        if self._stopped:
            return
        self.hass.bus.async_fire(EVENT_LEAK_DETECTED, {"activity_min": self.activity})
        await self._notify(
            "💧 Water leak detected",
            f"💧 {self.meter_display_name} has been flowing nearly non-stop "
            f"for over {self.activity:.0f} minutes. Check toilets, dishwasher, "
            "washing machine, water heater and outdoor taps!",
            "water_leak_alert",
        )

    async def _send_resolved(self, note: str | None = None) -> None:
        if self._stopped:
            return
        self.hass.bus.async_fire(EVENT_LEAK_RESOLVED)
        if note is None:
            title = "✅ Water leak resolved"
            message = (
                f"✅ No water flow on {self.meter_display_name} for over "
                f"{self.quiet_min} minutes. Leak assumed resolved."
            )
        else:
            # A counter reset ends the episode for a different reason; the
            # title should say so rather than claim the flow simply stopped.
            title = "♻️ Water leak resolved"
            message = note
        await self._notify(title, message, "water_leak_resolved")

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
        if self._stopped or self.leak_active or self.suppressed:
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
        if not self.leak_active or self.suppressed or self._stopped:
            # The leak was retracted (counter reset), Suppress Alerts was
            # switched on, or the entry unloaded while the save was awaited;
            # do not announce a leak the current state no longer warrants.
            if self._stopped and self.leak_active:
                # Unload must not leave a synthetic leak on disk for the next
                # setup to load as real evidence.
                self.leak_active = False
                self.activity = 0.0
                await self._save()
            return False
        try:
            await self._send_alert()
        except Exception:
            _LOGGER.exception("Simulated leak: could not send alert")
        return True

    async def _send_signal_lost(self) -> None:
        if self._stopped:
            return
        self.hass.bus.async_fire(
            EVENT_SIGNAL_LOST,
            {"water_meter": self.water_meter, "stale_min": self.stale_min},
        )
        await self._notify(
            "📡 Water meter signal lost",
            f"📡 No reading from {self.meter_display_name} for over "
            f"{self.stale_min} minutes. Check the bridge/receiver power and "
            "range, then reset the integration if it stays down.",
            "water_leak_signal_lost",
        )

    async def _send_signal_restored(self) -> None:
        if self._stopped:
            return
        self.hass.bus.async_fire(EVENT_SIGNAL_RESTORED)
        await self._notify(
            "📶 Water meter signal restored",
            f"📶 {self.meter_display_name} is reporting again.",
            "water_leak_signal_restored",
        )

    async def _notify(self, title: str, message: str, notification_id: str) -> None:
        if self._stopped or not self.notify_service:
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
            # blocking=True is essential: without it HA schedules the provider
            # in a background task and returns immediately, so this await would
            # never observe a delivery failure or a hung provider — the
            # fallback below would be unreachable and a failed alert would be
            # silently lost. The timeout provides the bound.
            await asyncio.wait_for(
                self.hass.services.async_call(
                    domain, service, call_data, blocking=True
                ),
                timeout=10,
            )
        except Exception as err:
            if self._stopped:
                return
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
        try:
            return dt_util.utc_from_timestamp(self.last_pulse_ts)
        except (OverflowError, OSError, ValueError):
            # A corrupt store can hold a finite but unrepresentable timestamp;
            # an entity update must not take platform setup down with it.
            _LOGGER.warning(
                "Ignoring out-of-range last_pulse_ts %s for %s",
                self.last_pulse_ts,
                self.entry.entry_id,
            )
            return None

    @property
    def min_detectable_leak_l_day(self) -> float:
        """Smallest continuous leak this meter can surface, given pulse size."""
        return round(
            self.pulse_ft3 * LITERS_PER_CUBIC_FOOT * 24 * 60 / self.quiet_min, 1
        )

    def add_entity(self, entity: Any) -> None:
        self._entities.append(entity)
        if self._stopped:
            return
        entity.update_from_hub()
        entity.async_write_ha_state()

    def _notify_entities(self) -> None:
        if self._stopped:
            return
        for entity in self._entities:
            entity.update_from_hub()
            entity.async_write_ha_state()

    def remove_entity(self, entity: Any) -> None:
        if entity in self._entities:
            self._entities.remove(entity)

    def async_shutdown(self) -> None:
        # Invalidate any callback already past its await points: after unload
        # the hub must not fire alerts, events, or entity writes.
        self._stopped = True
        if self._unsub_track is not None:
            self._unsub_track()
        if self._timer is not None:
            self._timer()
        if self._stale_timer is not None:
            self._stale_timer()