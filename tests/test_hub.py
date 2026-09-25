"""Unit tests for the WaterLeakHub detection logic."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from conftest import (
    FakeHass,
    FakeServices,
    StoreStub,
    make_entry,
    make_hub,
    notifications,
    timers,
    tracker,
)


def event(state: object) -> NS:
    return NS(data={"new_state": NS(state=str(state))})


def empty_event() -> NS:
    return NS(data={"new_state": None})


async def pump(hub, clock, pulses):
    for minutes, value in pulses:
        clock.now = minutes * 60.0
        await hub._on_meter_change(event(value))


def last_notify_data(hub):
    """Payload of the most recent notify service call made by the hub."""
    return hub.hass.services.calls[-1][2]


# --- helpers: _is_number / _parse_notify_data ------------------------------

def test_is_number(wl):
    assert wl._is_number("123.0") is True
    assert wl._is_number("0") is True
    assert wl._is_number("1e10") is True
    assert wl._is_number("abc") is False
    assert wl._is_number("") is False
    assert wl._is_number(None) is False
    assert wl._is_number("nan") is False
    assert wl._is_number("inf") is False


def test_parse_notify_data(wl, caplog):
    assert wl._parse_notify_data("") == {}
    assert wl._parse_notify_data(None) == {}
    assert wl._parse_notify_data('{"chat_id": 7}') == {"chat_id": 7}
    assert wl._parse_notify_data("not json") == {}
    assert "Invalid notify_data" in caplog.text
    assert wl._parse_notify_data("[1, 2]") == {}
    assert "must be a JSON object" in caplog.text


# --- hub construction / config parsing --------------------------------------

def test_hub_defaults(wl):
    hub = make_hub()
    assert hub.quiet_min == 45
    assert hub.limit_min == 120
    assert hub.pulse_ft3 == 2.0
    assert hub.stale_min == 180
    assert hub.notify_service == "notify.telegram"
    assert hub.notify_data == {}
    assert hub.water_meter == "sensor.meter"
    assert hub.last_value is None
    assert hub.last_pulse_ts is None
    assert hub.activity == 0.0
    assert hub.leak_active is False
    assert hub.suppressed is False
    assert hub.signal_lost is False


def test_hub_custom_options(wl):
    hub = make_hub(
        water_meter="sensor.water",
        quiet_min=30,
        limit_min=60,
        pulse_ft3=0.5,
        stale_min=0,
        notify_service=" notify.telegram ",  # whitespace must be stripped
        notify_data='{"chat_id": 9}',
    )
    assert hub.water_meter == "sensor.water"
    assert hub.quiet_min == 30
    assert hub.limit_min == 60
    assert hub.pulse_ft3 == 0.5
    assert hub.stale_min == 0
    assert hub.notify_service == "notify.telegram"
    assert hub.notify_data == {"chat_id": 9}


def test_last_pulse_dt(wl):
    hub = make_hub()
    assert hub.last_pulse_dt is None
    hub.last_pulse_ts = 1700000000.0
    dt = hub.last_pulse_dt
    assert dt is not None and dt.tzinfo is not None
    assert dt.year == 2023  # 2023-11-14


def test_min_detectable_leak_l_day(wl):
    hub = make_hub()
    assert hub.min_detectable_leak_l_day == 1812.3
    hub = make_hub(pulse_ft3=0.5, quiet_min=240)
    assert hub.min_detectable_leak_l_day == 85.0  # 0.5 ft³/240 min


def test_badly_formed_options_are_sanitized(wl):
    hub = make_hub(
        quiet_min=0,
        limit_min="abc",
        pulse_ft3="",
        stale_min=-5,
        notify_service=1,
        notify_data="not-json",
    )
    assert hub.quiet_min == 5  # clamped to the config-flow minimum
    assert hub.limit_min == 120  # non-numeric falls back to the default
    assert hub.pulse_ft3 == 2.0
    assert hub.stale_min == 0  # negative clamps to "disabled"
    assert hub.notify_service == ""  # non-string option must not crash
    assert hub.notify_data == {}
    assert make_hub(pulse_ft3=0.0).pulse_ft3 == 0.1  # clamped positive
    # absurd values must not reach async_call_later as an unrepresentable delay
    huge = make_hub(quiet_min=10**308, limit_min=10**308, pulse_ft3=10**308, stale_min=10**308)
    assert (huge.quiet_min, huge.limit_min, huge.pulse_ft3, huge.stale_min) == (
        240,
        1440,
        50.0,
        1440,
    )


def test_zero_quiet_min_cannot_divide_by_zero(wl):
    hub = make_hub(quiet_min=0)
    assert hub.quiet_min == 5
    assert hub.min_detectable_leak_l_day > 0  # sensor extra-attr stays finite


def test_non_finite_options_fall_back_to_defaults(wl):
    hub = make_hub(quiet_min="inf", limit_min="nan", pulse_ft3="inf", stale_min="-inf")
    assert hub.quiet_min == 45
    assert hub.limit_min == 120
    assert hub.pulse_ft3 == 2.0
    assert hub.stale_min == 180
    # a JSON int too large for a float must not raise out of coercion
    huge = make_hub(quiet_min=10**1000, limit_min=10**1000)
    assert huge.quiet_min == 45
    assert huge.limit_min == 120


async def test_async_load_rejects_malformed_store(wl, caplog):
    hub = make_hub()
    hub._store.data = ["not", "a", "mapping"]
    await hub.async_load()
    assert hub.last_value is None
    assert hub.activity == 0.0
    assert hub.leak_active is False
    assert "malformed stored state" in caplog.text


async def test_async_load_coerces_bad_store_field_types(wl):
    hub = make_hub()
    hub._store.data = {
        "last_value": "not-a-number",
        "last_pulse_ts": "nope",
        "activity": "bad",
        "leak_active": "false",  # truthy string must NOT become True
        "signal_lost": 1,
        "unavailable_since": "bad",
    }
    await hub.async_load()
    assert hub.last_value == 0.0
    # the unparseable clock is replaced by a re-anchor: with no attribution
    # (legacy store) the interval since it is undatable
    assert hub.last_pulse_ts > 0.0
    assert hub.activity == 0.0
    assert hub.leak_active is False
    assert hub.signal_lost is False
    assert hub.unavailable_since == 0.0


async def test_async_load_discards_state_when_meter_changed(wl):
    hub = make_hub()
    hub._store.data = {
        "water_meter": "sensor.old_meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "activity": 119.5,
        "leak_active": True,
        "signal_lost": True,
        "unavailable_since": 10.0,
    }
    await hub.async_load()
    assert hub.last_value is None
    assert hub.last_pulse_ts is None
    assert hub.activity == 0.0
    assert hub.leak_active is False
    assert hub.signal_lost is False
    assert hub.unavailable_since is None


async def test_async_load_keeps_state_for_same_meter(wl):
    hub = make_hub()
    hub._store.data = {"water_meter": "sensor.meter", "last_value": 42.0}
    await hub.async_load()
    assert hub.last_value == 42.0


# --- persistence: async_load / _save ---------------------------------------

async def test_async_load_empty(wl):
    hub = make_hub()
    await hub.async_load()
    assert hub.last_value is None
    assert hub.activity == 0.0
    assert hub.leak_active is False
    assert hub.signal_lost is False
    assert hub._timer is None
    assert hub._stale_timer is None


async def test_async_load_persisted_state(wl, clock):
    hub = make_hub()
    clock.now = 150.0  # last pulse 123 was 27 min ago (< 45 min quiet)
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 100.0,
        "last_pulse_ts": 123.0,
        "activity": 55.0,
        "leak_active": True,
        "signal_lost": True,
        "unavailable_since": 42.0,
    }
    await hub.async_load()
    assert hub.last_value == 100.0
    # attributed to this meter, so the stored cadence is trusted
    assert hub.last_pulse_ts == 123.0
    assert hub.activity == 55.0
    assert hub.signal_lost is True
    assert hub.unavailable_since == 42.0


async def test_async_load_persisted_leak_arms_watchdog(wl):
    hub = make_hub()
    hub._store.data = {"water_meter": "sensor.meter", "leak_active": True, "activity": 10.0}
    hub.hass.states.set("sensor.meter", "100")
    assert hub._timer is None
    await hub.async_load()
    assert hub.leak_active is True
    assert hub._timer is not None, "boot must arm the quiet watchdog for a persisted leak"
    await timers.fire_last()
    assert hub.leak_active is False


async def test_persisted_leak_stands_until_the_meter_returns(wl, clock):
    """Silence from a meter that is not present proves nothing — but not forever.

    A boot with the meter entity absent leaves the leak standing (no quiet
    timer, so no false resolution). When the meter reports an unchanged value
    it has proved no water was consumed, so the quiet window is armed from that
    reading and the leak resolves on real quiet time instead of staying stuck on.
    """
    hub = make_hub()
    clock.now = 600.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "activity": 120.0,
        "leak_active": True,
    }
    assert hub.hass.states.get("sensor.meter") is None
    await hub.async_load()
    assert hub.leak_active is True
    assert hub._timer is None, "no evidence, no quiet timer"

    # the meter comes back reporting the same value: no flow, so the window
    # must be armed from now rather than resumed from the pre-restart clock
    clock.now = 700.0
    await hub._on_meter_change(event(5000.0))
    assert hub._timer is not None, "an unchanged reading must arm the window"
    assert hub.leak_active is True, "not resolved on the same reading"

    clock.now = 700.0 + 46.0 * 60.0
    await timers.fire_last()
    assert hub.leak_active is False
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_RESOLVED, None)


async def test_save_persists_full_state(wl):
    hub = make_hub()
    hub.last_value = 5.0
    hub.last_pulse_ts = 6.0
    hub.activity = 1.0
    hub.leak_active = True
    hub.signal_lost = True
    hub.unavailable_since = 2.0
    await hub._save()
    assert hub._store.data == {
        "water_meter": "sensor.meter",
        "last_value": 5.0,
        "last_pulse_ts": 6.0,
        "activity": 1.0,
        "leak_active": True,
        "signal_lost": True,
        "unavailable_since": 2.0,
    }
    fresh = make_hub()
    fresh._store.data = dict(hub._store.data)
    await fresh.async_load()
    assert (fresh.last_value, fresh.last_pulse_ts, fresh.activity) == (5.0, 6.0, 1.0)


# --- boot-time state recovery -----------------------------------------------

async def test_check_initial_state_meter_missing(wl):
    hub = make_hub()
    await hub.async_load()
    assert hub.unavailable_since is None
    assert hub._stale_timer is None


async def test_check_initial_state_meter_reporting_idle(wl):
    hass = FakeHass()
    hass.states.set("sensor.meter", "123.0")
    hub = make_hub(hass=hass)
    await hub.async_load()
    assert hub.signal_lost is False
    assert hub.unavailable_since is None
    assert hub._stale_timer is None


async def test_check_initial_state_recovers_signal(wl):
    hass = FakeHass()
    hass.states.set("sensor.meter", "123.0")
    hub = make_hub(hass=hass)
    hub._store.data = {"signal_lost": True, "unavailable_since": 1.0}
    notifications.clear()
    await hub.async_load()
    await asyncio.sleep(0)
    assert hub.signal_lost is False
    assert hub.unavailable_since is None
    assert last_notify_data(hub)["title"] == "📶 Water meter signal restored"


async def test_check_initial_state_offline_rearms_clock(wl, clock):
    hass = FakeHass()
    hass.states.set("sensor.meter", "unavailable")
    clock.now = 100.0
    hub = make_hub(hass=hass)
    await hub.async_load()
    assert hub.unavailable_since == 100.0
    assert hub._stale_timer is not None


async def test_check_initial_state_offline_keeps_clock_and_rearms(wl, clock):
    """A mid-outage restart must keep the outage clock and re-arm the stale check."""
    hass = FakeHass()
    hass.states.set("sensor.meter", "unavailable")
    clock.now = 9100.0  # 150 min into the outage (since = 100 s)
    hub = make_hub(hass=hass)
    hub._store.data = {"unavailable_since": 100.0}
    await hub.async_load()
    assert hub.unavailable_since == 100.0  # preserved, not reset
    assert hub._stale_timer is not None  # re-armed for the remaining 30 min
    assert timers.last().delay == 30 * 60


async def test_check_initial_state_offline_past_deadline_fires(wl, clock):
    """A restart after the stale window has already expired must alarm promptly."""
    hass = FakeHass()
    hass.states.set("sensor.meter", "unavailable")
    clock.now = 20000.0  # ~331 min after the outage began
    hub = make_hub(hass=hass)
    hub._store.data = {"signal_lost": False, "unavailable_since": 100.0}
    notifications.clear()
    await hub.async_load()
    assert hub._stale_timer is not None  # zero-delay timer armed
    assert timers.last().delay == 0.0
    await timers.fire_last()
    assert hub.signal_lost is True


async def test_async_load_stale_activity_is_reset(wl, clock):
    """Quiet since the last pulse: stale activity must not survive a restart."""
    hub = make_hub()
    clock.now = 100.0 * 60.0
    hub._store.data = {
        "last_value": 100.0,
        "last_pulse_ts": 0.0,  # pulse was 100 min ago, quiet_min is 45
        "activity": 55.0,
    }
    await hub.async_load()
    assert hub.activity == 0.0
    assert hub._store.data["activity"] == 0.0  # persisted, stays reset after reboot


async def test_boot_clears_persisted_outage_clock_when_meter_up(wl, clock):
    """A restart while the meter already recovered must not leave a live clock.

    Otherwise the next outage's _on_unavailable sees a non-None clock, bails,
    and never arms a stale check: signal-loss detection dies silently.
    """
    hub = make_hub()
    clock.now = 200.0
    hub._store.data = {
        "last_value": 100.0,
        "unavailable_since": 60.0,  # outage started, then recovered pre-restart
    }
    hub.hass.states.set("sensor.meter", "123")
    await hub.async_load()
    assert hub.unavailable_since is None
    assert hub._store.data["unavailable_since"] is None
    # the next real outage must arm a fresh stale check
    clock.now = 300.0
    await hub._on_meter_change(event("unavailable"))
    assert hub.unavailable_since == 300.0
    assert hub._stale_timer is not None


async def test_boot_rearms_outage_when_meter_entity_missing(wl, clock):
    """No live entity but a persisted outage: the stale check must still arm.

    Otherwise the outage loses its timer, and the next unavailable event finds
    the clock already set and does nothing — signal loss goes unreported.
    """
    hub = make_hub(stale_min=5)
    clock.now = 400.0
    hub._store.data = {"unavailable_since": 100.0}  # 300s = 5 min elapsed
    assert hub.hass.states.get("sensor.meter") is None
    await hub.async_load()
    assert hub.unavailable_since == 100.0
    assert hub._stale_timer is not None, "the outage must keep its stale check"


async def test_leak_alert_suppressed_when_counter_reset_races_the_save(wl, clock):
    """A counter reset during the crossing pulse's save must retract the alert.

    Otherwise the bus records resolved-then-detected while the final state is
    not-leaking: an alert for a leak that no longer exists.
    """
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False
            self.saw_active = False

        async def async_save(self, data):
            if data.get("leak_active") and not self.injected:
                self.injected = True
                # a dramatic drop lands while the crossing pulse is saving
                await self.hub_ref._on_meter_change(event(1.0))
            self.saw_active = self.saw_active or bool(data.get("leak_active"))
            await super().async_save(data)

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    hub.last_value = 5000.0
    hub.last_pulse_ts = 0.0
    hub.activity = 119.0
    clock.now = 40.0 * 60.0
    await hub._on_meter_change(event(5001.0))  # gap 40 < 45 -> crosses the limit
    assert hub.leak_active is False, "the reset retracted the leak"
    assert not any(
        e[0] == wl.EVENT_LEAK_DETECTED for e in hub.hass.bus.fired
    ), "must not announce a retracted leak"


async def test_leak_alert_suppressed_when_suppression_turns_on_during_save(wl, clock):
    """Suppression enabled while the crossing pulse saves must silence the alert."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False

        async def async_save(self, data):
            if data.get("leak_active") and not self.injected:
                self.injected = True
                self.hub_ref.set_suppressed(True)
            await super().async_save(data)

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    hub.last_value = 100.0
    hub.last_pulse_ts = 0.0
    hub.activity = 119.0
    clock.now = 40.0 * 60.0
    await hub._on_meter_change(event(101.0))
    assert hub.suppressed is True
    assert not notifications, "an alert must not fire after Suppress Alerts is on"
    assert not any(e[0] == wl.EVENT_LEAK_DETECTED for e in hub.hass.bus.fired)


async def test_resolve_preserves_pulse_that_landed_mid_save(wl, clock):
    """A pulse during the resolve-save must not have its activity clobbered."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False
            self.prev_leak = False

        async def async_save(self, data):
            if self.prev_leak and not data.get("leak_active") and not self.injected:
                self.injected = True
                # a short pulse arrives: it refreshes the cadence but stays
                # under the limit, so the leak is not re-raised
                await self.hub_ref._on_meter_change(event(self.hub_ref.last_value + 1))
            self.prev_leak = bool(data.get("leak_active"))
            await super().async_save(data)

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    clock.now = 164.0 * 60.0  # 44 min gap: accumulates, stays under 120
    await timers.fire_last()
    assert hub.leak_active is False
    assert hub.activity == 44.0, "the mid-save pulse's activity must survive"
    assert hub._store.data["activity"] == 44.0


async def test_boot_changed_value_reanchors_instead_of_charging_downtime(wl, clock):
    """A value that moved while HA was down cannot date the increment.

    The meter is a passive accumulator: an unchanged value at boot really does
    prove no pulse arrived, so that gap stays countable. A CHANGED value means
    the increment happened at an unknown moment during the downtime, and
    charging the whole blind interval manufactures a leak.
    """
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,  # 40 min ago, under quiet_min
        "activity": 119.0,  # one minute under the limit
    }
    hub.hass.states.set("sensor.meter", "5002")  # moved while we were down
    await hub.async_load()
    assert hub.leak_active is False
    assert hub.last_pulse_ts == 40.0 * 60.0
    # real pre-restart evidence is kept; only the undatable gap is dropped
    assert hub.activity == 119.0
    clock.now = 80.0 * 60.0
    await hub._on_meter_change(event(5003.0))
    assert hub.activity == 159.0  # 119 kept + 40 real minutes
    assert hub.leak_active is True, "a near-threshold episode must survive a restart"


async def test_boot_keeps_gap_when_meter_value_unchanged(wl, clock):
    """Same value at boot means nothing was missed, so the gap is real."""
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "activity": 119.0,
    }
    hub.hass.states.set("sensor.meter", "5000")
    await hub.async_load()
    assert hub.activity == 119.0, "an unchanged reading proves the gap is real"
    assert hub.last_pulse_ts == 0.0


async def test_boot_legacy_state_without_meter_reanchors(wl, clock):
    """Pre-water_meter stores cannot be attributed, so the clock is re-anchored."""
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {"last_value": 5000.0, "last_pulse_ts": 0.0, "activity": 119.0}
    hub.hass.states.set("sensor.meter", "5002")
    await hub.async_load()
    assert hub.last_value == 5002.0
    assert hub.last_pulse_ts == 40.0 * 60.0
    assert hub.activity == 119.0, "a pulse during downtime keeps the evidence"


async def test_boot_legacy_store_does_not_inherit_a_counter_resets_activity(wl, clock):
    """A legacy store must get the same counter-reset handling as an attributed one."""
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {"last_value": 5000.0, "last_pulse_ts": 0.0, "activity": 119.0}
    hub.hass.states.set("sensor.meter", "3")  # far lower: a reset, not a pulse
    await hub.async_load()
    assert hub.last_value == 3.0
    assert hub.activity == 0.0, "the old counter's evidence must not carry over"
    # one real pulse later there is no inherited activity to tip over
    clock.now = 80.0 * 60.0
    await hub._on_meter_change(event(4.0))
    assert hub.activity == 40.0
    assert hub.leak_active is False


async def test_boot_legacy_store_keeps_evidence_across_a_long_outage(wl, clock):
    """Long downtime plus a changed reading: the near-threshold leak survives."""
    hub = make_hub()
    clock.now = 100.0 * 60.0  # far past quiet_min
    hub._store.data = {"last_value": 5000.0, "last_pulse_ts": 0.0, "activity": 119.0}
    hub.hass.states.set("sensor.meter", "5002")
    await hub.async_load()
    assert hub.activity == 119.0, (
        "a pulse landed during the outage, so the evidence is not stale"
    )
    clock.now = 140.0 * 60.0
    await hub._on_meter_change(event(5003.0))
    assert hub.leak_active is True, "the legacy path must match the attributed one"


async def test_boot_persisted_leak_resumes_remaining_quiet_window(wl, clock):
    """A restart must not hand an old leak a fresh full quiet window."""
    hub = make_hub()
    clock.now = 40.0 * 60.0  # the quiet window (45 min) is nearly spent
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_pulse_ts": 0.0,
        "leak_active": True,
        "activity": 120.0,
    }
    hub.hass.states.set("sensor.meter", "100")
    await hub.async_load()
    assert timers.handles[-1].delay == 5 * 60, "only the remainder is left"


async def test_boot_expired_leak_resolves_immediately(wl, clock):
    hub = make_hub()
    clock.now = 600.0 * 60.0  # long past the quiet window
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_pulse_ts": 0.0,
        "leak_active": True,
        "activity": 120.0,
    }
    hub.hass.states.set("sensor.meter", "100")
    await hub.async_load()
    assert timers.handles[-1].delay == 0
    await timers.fire_last()
    assert hub.leak_active is False


async def test_entity_removal_starts_the_outage_clock(wl, clock):
    """HA reports a removed entity as new_state=None; that is an outage."""
    hub = make_hub()
    await hub.async_load()
    clock.now = 60.0
    await hub._on_meter_change(empty_event())
    assert hub.unavailable_since == 60.0
    assert hub._stale_timer is not None


async def test_boot_legacy_state_with_active_leak_keeps_evidence(wl, clock):
    """A legacy store with an active leak keeps its evidence, but re-anchors."""
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "leak_active": True,
        "activity": 120.0,
    }
    hub.hass.states.set("sensor.meter", "5002")
    await hub.async_load()
    assert hub.leak_active is True, "real evidence must not be discarded"
    assert hub.activity == 120.0
    assert hub.last_value == 5002.0
    assert hub.last_pulse_ts == 40.0 * 60.0


async def test_resolve_stale_when_second_pulse_rearms_the_leak(wl, clock):
    """A leak re-armed during the corrective save must not also get 'resolved'.

    The first save triggers the corrective re-save (a pulse bumped the watchdog
    sequence); if the leak comes back during that second await, this resolve is
    stale and must stand down.
    """
    hub = make_hub()

    class InjectingStore(StoreStub):
        """Re-arms the watchdog on the resolve-save, then the leak on the re-save."""

        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.saves = 0

        async def async_save(self, data):
            await super().async_save(data)
            if not self.armed or data.get("leak_active"):
                return
            self.saves += 1
            if self.saves == 1:
                self.hub_ref._watchdog_seq += 1  # a pulse landed: take the re-save
            elif self.saves == 2:
                self.hub_ref.leak_active = True  # and it re-raised the leak

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    hub._store.armed = True
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert hub.leak_active is True, "the re-armed leak owns the state"
    assert not any(e[0] == wl.EVENT_LEAK_RESOLVED for e in hub.hass.bus.fired)


async def test_last_pulse_dt_ignores_out_of_range_timestamp(wl, caplog):
    hub = make_hub()
    hub.last_pulse_ts = 1e308
    assert hub.last_pulse_dt is None
    assert "out-of-range" in caplog.text


async def test_simulate_leak_suppressed_when_suppression_turns_on_during_save(wl):
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False

        async def async_save(self, data):
            if data.get("leak_active") and not self.injected:
                self.injected = True
                self.hub_ref.set_suppressed(True)
            await super().async_save(data)

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    assert await hub._simulate_leak() is False
    assert not notifications
    assert not any(e[0] == wl.EVENT_LEAK_DETECTED for e in hub.hass.bus.fired)


async def test_simulate_leak_does_not_mutate_a_stopped_hub(wl):
    hub = make_hub()
    await hub.async_load()
    hub.async_shutdown()
    n_timers = len(timers)
    assert await hub._simulate_leak() is False
    assert hub.leak_active is False
    assert hub.activity == 0.0
    assert len(timers) == n_timers
    assert hub._store.data.get("leak_active") is not True


async def test_notify_fallback_skipped_after_shutdown(wl):
    hub = make_hub(notify_service="notify.telegram")

    class ShutdownThenFail(FakeServices):
        async def async_call(self, domain, service, service_data, **kwargs):
            hub.async_shutdown()
            raise RuntimeError("boom")

    hub.hass.services = ShutdownThenFail({"notify": {"telegram": True}})
    notifications.clear()
    await hub._notify("Water leak detected", "msg", "water_leak_alert")
    assert not notifications, "a stopped hub must not publish a fallback"


async def test_concurrent_pulse_callbacks_do_not_write_a_stale_baseline(wl, clock):
    """HA runs state callbacks as concurrent tasks; the older one must yield."""
    hub = make_hub()

    class GatedStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.gate = asyncio.Event()
            self.armed = False
            self.blocks = 0

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and self.blocks < 2:
                self.blocks += 1
                await self.gate.wait()

    hub._store = GatedStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    clock.now = 0.0
    await hub._on_meter_change(event(100.0))
    hub._store.armed = True
    clock.now = 10.0 * 60.0
    first = asyncio.create_task(hub._on_meter_change(event(101.0)))
    await asyncio.sleep(0)  # it reaches the gated save after _on_available
    clock.now = 20.0 * 60.0
    newer = asyncio.create_task(hub._on_meter_change(event(102.0)))
    await asyncio.sleep(0)
    hub._store.gate.set()
    await first
    await newer
    assert hub.last_value == 102.0, "the newer baseline must win"


async def test_recovery_callback_yields_to_a_newer_reading(wl, clock):
    """A recovery callback suspended inside _on_available must yield."""
    hub = make_hub()

    class GatedStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.gate = asyncio.Event()
            self.armed = False
            self.blocks = 0

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and self.blocks < 1:
                self.blocks += 1
                await self.gate.wait()

    hub._store = GatedStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    hub.last_value = 100.0
    hub.last_pulse_ts = 0.0
    clock.now = 10.0 * 60.0
    await hub._on_meter_change(event("unavailable"))  # starts an outage
    hub._store.armed = True
    clock.now = 20.0 * 60.0
    older = asyncio.create_task(hub._on_meter_change(event(101.0)))
    await asyncio.sleep(0)  # suspended inside _on_available's save
    clock.now = 25.0 * 60.0
    await hub._on_meter_change(event(102.0))
    hub._store.gate.set()
    await older
    assert hub.last_value == 102.0, "the stale recovery must not win"
    assert hub.last_pulse_ts == 25.0 * 60.0


async def test_concurrent_dip_callback_does_not_overwrite_a_newer_reading(wl, clock):
    """The same guard must hold on the counter-reset/dip path."""
    hub = make_hub()

    class GatedStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.gate = asyncio.Event()
            self.armed = False
            self.blocks = 0

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and self.blocks < 2:
                self.blocks += 1
                await self.gate.wait()

    hub._store = GatedStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    hub.last_value = 5000.0
    clock.now = 0.0
    hub._store.armed = True
    older = asyncio.create_task(hub._on_meter_change(event(20.0)))  # big drop
    await asyncio.sleep(0)
    clock.now = 10.0 * 60.0
    newer = asyncio.create_task(hub._on_meter_change(event(5001.0)))
    await asyncio.sleep(0)
    hub._store.gate.set()
    await older
    await newer
    assert hub.last_value == 5001.0, "the stale drop must not win"
    assert hub.leak_active is False


async def test_active_leak_boot_restarts_window_when_meter_moved(wl, clock):
    """A pulse during downtime means flow is ongoing: the window restarts."""
    hub = make_hub(quiet_min=5)
    clock.now = 4.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "leak_active": True,
        "activity": 120.0,
    }
    hub.hass.states.set("sensor.meter", "5001")  # moved while we were down
    await hub.async_load()
    assert hub.leak_active is True
    assert hub.last_pulse_ts == 4.0 * 60.0, "the window restarts from the live reading"
    # the delay is the distinguishing check: resuming the stale deadline would
    # schedule 1 min (300-240) instead of a full window from the live pulse
    assert timers.handles[-1].delay == 5 * 60, "a full window, not the stale remainder"


async def test_active_leak_boot_resumes_remainder_when_meter_unchanged(wl, clock):
    hub = make_hub(quiet_min=45)
    clock.now = 40.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "leak_active": True,
        "activity": 120.0,
    }
    hub.hass.states.set("sensor.meter", "5000")  # no pulse during downtime
    await hub.async_load()
    assert timers.handles[-1].delay == 5 * 60


async def test_quiet_resolve_stands_down_when_outage_starts_mid_save(wl, clock):
    """A meter going dark during the resolve-save must not yield 'resolved'."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.injected = False

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and not data.get("leak_active") and not self.injected:
                self.injected = True
                await self.hub_ref._on_meter_change(event("unavailable"))

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    hub._store.armed = True
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert hub.leak_active is True, "silence without signal proves nothing"
    assert not any(e[0] == wl.EVENT_LEAK_RESOLVED for e in hub.hass.bus.fired)
    assert not notifications


async def test_signal_lost_stands_down_when_meter_recovers_mid_save(wl, clock):
    """Recovery during the signal-lost save must not also announce a loss."""
    hub = make_hub(stale_min=5)

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.injected = False

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and data.get("signal_lost") and not self.injected:
                self.injected = True
                await self.hub_ref._on_meter_change(event(7.0))

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    hub._store.armed = True
    clock.now = 60.0 + 5.0 * 60.0
    await timers.fire_last()
    assert hub.signal_lost is False, "recovery won"
    assert not any(e[0] == wl.EVENT_SIGNAL_LOST for e in hub.hass.bus.fired)


async def test_counter_reset_warns_exactly_once(wl, caplog, clock):
    """The dramatic-drop branch must log its own warning (not the dip one)."""
    hub = make_hub()
    await hub.async_load()
    caplog.clear()
    hub.last_value = 100.0
    clock.now = 10.0 * 60.0
    await hub._on_meter_change(event(10.0))  # dramatic drop
    text = caplog.text
    assert text.count("assuming counter reset") == 1
    assert "re-anchoring baseline" not in text


async def test_small_dip_warns_only_the_dip(wl, caplog, clock):
    hub = make_hub()
    await hub.async_load()
    caplog.clear()
    hub.last_value = 100.0
    clock.now = 10.0 * 60.0
    await hub._on_meter_change(event(60.0))  # small dip
    text = caplog.text
    assert "re-anchoring baseline" in text
    assert "assuming counter reset" not in text


async def test_corrupt_outage_clock_cannot_break_the_stale_timer(wl, clock):
    """A far-past clock means the deadline long passed: fire immediately."""
    hub = make_hub(stale_min=5)
    clock.now = 1000.0
    hub._store.data = {"unavailable_since": -1e300}
    hub.hass.states.set("sensor.meter", "unavailable")
    await hub.async_load()
    assert hub._stale_timer is not None
    assert timers.handles[-1].delay == 0, (
        "an unrepresentable delay would break the event loop's timers"
    )


async def test_notify_requests_blocking_delivery(wl):
    """Real HA returns immediately without blocking=True, hiding failures."""
    hub = make_hub(notify_service="notify.telegram")
    hub.hass.services = FakeServices({"notify": {"telegram": True}})
    await hub._notify("Water leak detected", "msg", "water_leak_alert")
    assert hub.hass.services.blocking_calls == [True]


async def test_setup_fails_without_a_water_meter(wl, caplog):
    from conftest import make_entry as _me

    entry = _me()
    entry.options["water_meter"] = None
    assert await wl.async_setup_entry(FakeHass(), entry) is False
    assert "No water meter configured" in caplog.text


async def test_setup_failure_does_not_leave_a_live_hub(wl):
    from conftest import make_entry as _me

    entry = _me()
    hass = FakeHass()

    async def boom(_entry, _platforms):
        raise RuntimeError("platform setup failed")

    hass.config_entries.async_forward_entry_setups = boom
    with pytest.raises(RuntimeError):
        await wl.async_setup_entry(hass, entry)
    assert hass.data.get(wl.DOMAIN) == {}
    assert tracker.unsubscribed == 1


async def test_failed_unload_keeps_the_hub(wl):
    entry = make_entry()
    hub = make_hub()
    hass = hub.hass
    hass.data[wl.DOMAIN] = {entry.entry_id: hub}

    async def refuse(_entry, _platforms):
        return False

    hass.config_entries.async_unload_platforms = refuse
    assert await wl.async_unload_entry(hass, entry) is False
    assert hass.data[wl.DOMAIN][entry.entry_id] is hub, "a failed unload keeps it live"
    assert tracker.unsubscribed == 0


async def test_outage_callback_does_not_strand_a_crossing_pulse(wl, clock):
    """A pulse waiting on a save must still fire even if an outage lands.

    The generation counter tracks committed baselines, so an outage callback
    (which commits nothing) must not invalidate real threshold evidence.
    """
    hub = make_hub()

    class GatedStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.gate = asyncio.Event()
            self.armed = False
            self.blocks = 0

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and self.blocks < 1:
                self.blocks += 1
                await self.gate.wait()

    hub._store = GatedStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    hub.last_value = 100.0
    hub.last_pulse_ts = 0.0
    hub.activity = 80.0  # 40 more minutes crosses the 120 limit
    hub._store.armed = True
    clock.now = 40.0 * 60.0
    crossing = asyncio.create_task(hub._on_meter_change(event(101.0)))
    await asyncio.sleep(0)
    clock.now = 41.0 * 60.0
    await hub._on_meter_change(event("unavailable"))  # commits nothing
    hub._store.gate.set()
    await crossing
    assert hub.activity >= 120.0
    assert hub.leak_active is True, "the real crossing must not be stranded"
    assert any(e[0] == wl.EVENT_LEAK_DETECTED for e in hub.hass.bus.fired)


async def test_boot_counter_reset_while_down_clears_the_leak(wl, clock):
    """A far-lower live value at boot is a reset, not a pulse."""
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "leak_active": True,
        "activity": 120.0,
    }
    notifications.clear()
    hub.hass.states.set("sensor.meter", "3")  # counter reset during downtime
    await hub.async_load()
    assert hub.leak_active is False
    assert hub.activity == 0.0
    assert hub.last_value == 3.0
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_RESOLVED, None)


async def test_boot_reanchors_legacy_state_without_a_live_entity(wl, clock):
    """No live entity and no attribution: the interval is undatable."""
    hub = make_hub()
    clock.now = 20.0 * 60.0
    hub._store.data = {"last_value": 5000.0, "last_pulse_ts": 0.0, "activity": 119.0}
    await hub.async_load()
    assert hub.last_pulse_ts == 20.0 * 60.0
    clock.now = 60.0 * 60.0
    await hub._on_meter_change(event(5002.0))
    assert hub.activity == 119.0 + 40.0, "only the post-boot gap is charged"
    assert hub.leak_active is True, "119 preserved + 40 real minutes crosses the limit"


async def test_meter_swap_is_persisted(wl, clock):
    """Switching meters must rewrite the store, not just memory."""
    hub = make_hub()
    hub._store.data = {
        "water_meter": "sensor.old",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "activity": 119.0,
    }
    await hub.async_load()
    assert hub._store.data["water_meter"] == "sensor.meter"
    assert hub._store.data["last_value"] is None
    assert hub._store.data["activity"] == 0.0


async def test_future_outage_clock_is_restarted(wl, clock):
    hub = make_hub()
    clock.now = 1000.0
    hub._store.data = {"unavailable_since": 1e308}
    hub.hass.states.set("sensor.meter", "unavailable")
    await hub.async_load()
    assert hub.unavailable_since == 1000.0
    assert hub._stale_timer is not None


async def test_preserved_activity_gets_a_watchdog(wl, clock):
    """Re-anchored evidence must still expire on real quiet time."""
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "activity": 119.0,
    }
    hub.hass.states.set("sensor.meter", "5002")
    await hub.async_load()
    assert hub.activity == 119.0
    assert hub._timer is not None, "stale evidence must not sit forever"
    clock.now = 85.0 * 60.0
    await timers.fire_last()
    assert hub.activity == 0.0


async def test_signal_restored_stands_down_when_outage_restarts_mid_save(wl, clock):
    hub = make_hub(stale_min=5)

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.injected = False

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and not data.get("signal_lost") and not self.injected:
                self.injected = True
                await self.hub_ref._on_meter_change(event("unavailable"))

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    clock.now = 60.0 + 5.0 * 60.0
    await timers.fire_last()  # signal lost
    assert hub.signal_lost is True
    hub._store.armed = True
    clock.now += 60.0
    await hub._on_meter_change(event(2.0))  # recovery
    assert hub.signal_lost is False
    assert hub.unavailable_since is not None
    assert not any(e[0] == wl.EVENT_SIGNAL_RESTORED for e in hub.hass.bus.fired)


async def test_simulate_leak_rolls_back_on_shutdown(wl):
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False

        async def async_save(self, data):
            await super().async_save(data)
            if data.get("leak_active") and not self.injected:
                self.injected = True
                self.hub_ref.async_shutdown()

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    assert await hub._simulate_leak() is False
    assert hub.leak_active is False
    assert hub._store.data["leak_active"] is False
    assert hub._store.data["activity"] == 0.0
    assert not notifications


async def test_water_meter_is_trimmed(wl):
    hub = make_hub(water_meter="  sensor.meter  ")
    assert hub.water_meter == "sensor.meter"


async def test_invalid_notify_json_is_not_logged_verbatim(wl, caplog):
    secret = '{"token": "s3cr3t-value", oops'
    hub = make_hub(notify_data=secret)
    assert hub.notify_data == {}
    assert "s3cr3t-value" not in caplog.text
    assert "Invalid notify_data JSON" in caplog.text


async def test_non_object_notify_json_is_not_logged_verbatim(wl, caplog):
    secret = '["s3cr3t-value"]'
    hub = make_hub(notify_data=secret)
    assert hub.notify_data == {}
    assert "s3cr3t-value" not in caplog.text


async def test_quiet_resolve_stands_down_when_outage_starts_during_resave(wl, clock):
    """An outage during the corrective re-save must also suppress 'resolved'."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.saves = 0

        async def async_save(self, data):
            await super().async_save(data)
            if not self.armed or data.get("leak_active"):
                return
            self.saves += 1
            if self.saves == 1:
                self.hub_ref._watchdog_seq += 1  # take the corrective path
            elif self.saves == 2:
                await self.hub_ref._on_meter_change(event("unavailable"))

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    hub._store.armed = True
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert not any(e[0] == wl.EVENT_LEAK_RESOLVED for e in hub.hass.bus.fired)
    assert not notifications


async def test_setup_failure_registers_no_update_listener(wl):
    entry = make_entry()
    hass = FakeHass()

    async def boom(_entry, _platforms):
        raise RuntimeError("platform setup failed")

    hass.config_entries.async_forward_entry_setups = boom
    with pytest.raises(RuntimeError):
        await wl.async_setup_entry(hass, entry)
    assert entry.listeners == [], "a failed setup must not leave a reload listener"


async def test_boot_counter_reset_while_down_stays_silent_after_shutdown(wl, clock):
    """A counter reset discovered at boot after unload must not announce."""
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "leak_active": True,
        "activity": 120.0,
    }
    hub.hass.states.set("sensor.meter", "unavailable")
    await hub.async_load()
    notifications.clear()
    hub.async_shutdown()
    hub.leak_active = True  # the flag the reset would have cleared
    hub.hass.states.set("sensor.meter", "3")  # a counter reset while down
    await hub._reanchor_after_restart()
    assert hub.leak_active is False
    assert not notifications
    assert not any(e[0] == wl.EVENT_LEAK_RESOLVED for e in hub.hass.bus.fired)


async def test_stale_dip_callback_yields_to_a_newer_reading(wl, clock):
    hub = make_hub()

    class GatedStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.gate = asyncio.Event()
            self.armed = False
            self.blocks = 0

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and self.blocks < 2:
                self.blocks += 1
                await self.gate.wait()

    hub._store = GatedStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    hub.last_value = 100.0
    hub._store.armed = True
    writes: list[int] = []
    hub._entities = [
        type(
            "E",
            (),
            {
                "update_from_hub": lambda s: None,
                "async_write_ha_state": lambda s: writes.append(1),
            },
        )()
    ]
    clock.now = 0.0
    older = asyncio.create_task(hub._on_meter_change(event(60.0)))  # small dip
    await asyncio.sleep(0)
    clock.now = 10.0 * 60.0
    newer = asyncio.create_task(hub._on_meter_change(event(101.0)))
    await asyncio.sleep(0)
    hub._store.gate.set()
    await older
    await newer
    assert hub.last_value == 101.0
    # the superseded dip must not republish: a newer callback owns the state
    assert writes == [1], "only the winning callback may write entity state"


async def test_outage_restoration_arms_nothing_after_shutdown(wl, clock):
    """A shutdown during the restore save must leave no new timer behind."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.saves = 0

        async def async_save(self, data):
            await super().async_save(data)
            if not self.armed or data.get("leak_active"):
                return
            self.saves += 1
            if self.saves == 1:
                # the outage starts; the entry is unloaded while the
                # restoration save is awaited
                self.hub_ref.unavailable_since = clock.now
                self.hub_ref.async_shutdown()

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    hub._store.armed = True
    n_timers = len(timers)
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert not notifications
    assert len(timers) == n_timers, "no watchdog may be armed after unload"


async def test_resave_outage_restoration_arms_nothing_after_shutdown(wl, clock):
    """Same, on the corrective re-save path."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.saves = 0

        async def async_save(self, data):
            await super().async_save(data)
            if not self.armed or data.get("leak_active"):
                return
            self.saves += 1
            if self.saves == 1:
                self.hub_ref._watchdog_seq += 1
            elif self.saves == 2:
                self.hub_ref.unavailable_since = clock.now
                self.hub_ref.async_shutdown()

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    hub._store.armed = True
    n_timers = len(timers)
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert not notifications
    assert len(timers) == n_timers, "no watchdog may be armed after unload"


async def test_stopped_hub_arms_no_watchdog(wl):
    """Every watchdog arming path must respect the stopped flag."""
    hub = make_hub()
    await hub.async_load()
    hub.async_shutdown()
    n_timers = len(timers)
    hub._schedule_watchdog()
    hub._schedule_watchdog(60)
    assert hub._timer is None
    assert len(timers) == n_timers


async def test_future_last_pulse_is_discarded(wl, clock, caplog):
    """A pulse clock from the future is corrupt and must not subtract activity."""
    hub = make_hub()
    clock.now = 100.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 1e308,
        "activity": 100.0,
    }
    await hub.async_load()
    assert hub.last_pulse_ts is None
    assert "in the future" in caplog.text
    clock.now = 160.0 * 60.0
    await hub._on_meter_change(event(5001.0))
    assert hub.activity == 0.0, "no negative gap may eat the evidence"


async def test_backwards_clock_does_not_subtract_activity(wl, clock):
    hub = make_hub()
    await hub.async_load()
    hub.last_value = 5000.0
    hub.last_pulse_ts = 10_000.0  # ahead of the patched clock
    hub.activity = 100.0
    clock.now = 60.0
    await hub._on_meter_change(event(5001.0))
    assert hub.activity == 100.0, "a backwards clock must not reduce activity"


async def test_superseded_pulse_still_evaluates_the_leak_threshold(wl, clock):
    """A callback superseded mid-save must not lose a real crossing."""
    hub = make_hub()

    class GatedStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.gate = asyncio.Event()
            self.armed = False
            self.blocks = 0

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and self.blocks < 1:
                self.blocks += 1
                await self.gate.wait()

    hub._store = GatedStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    hub.last_value = 100.0
    hub.last_pulse_ts = 0.0
    hub.activity = 80.0  # 40 more minutes crosses the 120 limit
    hub._store.armed = True
    clock.now = 40.0 * 60.0
    crossing = asyncio.create_task(hub._on_meter_change(event(101.0)))
    await asyncio.sleep(0)
    clock.now = 41.0 * 60.0
    dip = asyncio.create_task(hub._on_meter_change(event(60.0)))  # commits a dip
    await asyncio.sleep(0)
    hub._store.gate.set()
    await crossing
    await dip
    assert hub.leak_active is True, "a real crossing must not be lost"
    assert any(e[0] == wl.EVENT_LEAK_DETECTED for e in hub.hass.bus.fired)


async def test_quiet_reset_skipped_when_outage_starts_mid_save(wl, clock):
    """Pre-outage evidence must stand when the meter goes dark during the save."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.saves = 0

        async def async_save(self, data):
            await super().async_save(data)
            if not self.armed:
                return
            self.saves += 1
            if self.saves == 1:  # the quiet-reset save only
                self.hub_ref.unavailable_since = clock.now

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0)])
    assert hub.activity == 80.0
    hub._store.armed = True
    hub._store.saves = 0
    clock.now = 126.0 * 60.0
    await timers.fire_last()
    assert hub.unavailable_since is not None
    assert hub.activity == 80.0, "an outage means the evidence stands"
    assert hub._store.data["activity"] == 80.0


async def test_post_subscribe_reconcile_catches_a_change_during_load(wl, clock):
    """A meter change while setup was awaiting must be reconciled after it."""
    hub = make_hub()
    hub.hass.states.set("sensor.meter", "5000")
    await hub.async_load()
    assert hub.last_value == 5000.0
    # the meter moves while the first event is in flight
    hub.hass.states.set("sensor.meter", "5005")
    await hub._check_initial_state()
    assert await hub._reanchor_after_restart() is True
    assert hub.last_value == 5005, "the post-subscribe reconcile must catch the change"


async def test_unload_during_pulse_save_fires_nothing(wl, clock):
    """Shutting down while a pulse is saving must not alert afterwards."""
    hub = make_hub()

    class ShutdownStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.done = False

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and not self.done:
                self.done = True
                self.hub_ref.async_shutdown()

    hub._store = ShutdownStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    hub.last_value = 100.0
    hub.last_pulse_ts = 0.0
    hub.activity = 119.0  # one pulse away from the limit
    hub._store.armed = True
    clock.now = 40.0 * 60.0
    await hub._on_meter_change(event(101.0))
    assert not notifications
    assert not any(e[0] == wl.EVENT_LEAK_DETECTED for e in hub.hass.bus.fired)


async def test_notifications_use_the_entity_display_name(wl):
    """Alerts should name the meter, not print its entity id."""
    hub = make_hub()
    await hub.async_load()
    hub.hass.states.set("sensor.meter", "100", name="Basement Water Meter")
    assert hub.meter_display_name == "Basement Water Meter"
    await hub._send_alert()
    await hub._send_resolved()
    await hub._send_signal_lost()
    await hub._send_signal_restored()
    payloads = [c[2] for c in hub.hass.services.calls]
    titles = [p["title"] for p in payloads]
    assert titles == [
        "💧 Water leak detected",
        "✅ Water leak resolved",
        "📡 Water meter signal lost",
        "📶 Water meter signal restored",
    ]
    for payload in payloads:
        assert "Basement Water Meter" in payload["message"], payload
        assert "sensor.meter" not in payload["message"], payload
    assert any("💧" in p["message"] for p in payloads)
    assert any("✅" in p["message"] for p in payloads)
    assert any("📡" in p["message"] for p in payloads)
    assert any("📶" in p["message"] for p in payloads)


async def test_display_name_falls_back_to_entity_id(wl):
    hub = make_hub()
    await hub.async_load()
    assert hub.hass.states.get("sensor.meter") is None
    assert hub.meter_display_name == "sensor.meter"


async def test_concurrent_recovery_does_not_charge_the_blackout_as_flow(wl, clock):
    """Two recoveries in one tick must not bill the outage to the second.

    The first callback suspends inside _on_available's save; the second then
    observes the outage clock already cleared and would otherwise compute its
    gap from the pre-outage pulse, adding the whole blackout to activity.
    """
    hub = make_hub()

    class GatedStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.gate = asyncio.Event()
            self.armed = False
            self.blocks = 0

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and self.blocks < 1:
                self.blocks += 1
                await self.gate.wait()

    hub._store = GatedStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    clock.now = 0.0
    await hub._on_meter_change(event(100.0))
    hub.activity = 100.0
    clock.now = 10.0 * 60.0
    await hub._on_meter_change(event("unavailable"))
    hub._store.armed = True
    clock.now = 50.0 * 60.0  # 40 min of blackout
    first = asyncio.create_task(hub._on_meter_change(event(101.0)))
    await asyncio.sleep(0)  # first suspends inside _on_available's save
    second = asyncio.create_task(hub._on_meter_change(event(102.0)))
    await asyncio.sleep(0)
    hub._store.gate.set()
    await first
    await second
    assert hub.activity == 100.0, "the blackout must not be charged as flow"
    assert hub.leak_active is False
    assert not any(e[0] == wl.EVENT_LEAK_DETECTED for e in hub.hass.bus.fired)


async def test_counter_reset_resolves_even_when_superseded(wl, clock):
    """A newer reading mid-save must not swallow the counter-reset notice."""
    hub = make_hub()

    class GatedStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.gate = asyncio.Event()
            self.armed = False
            self.blocks = 0

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and self.blocks < 1:
                self.blocks += 1
                await self.gate.wait()

    hub._store = GatedStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    hub.last_value = 5000.0
    hub.last_pulse_ts = 0.0
    hub.activity = 120.0
    hub.leak_active = True
    hub._store.armed = True
    clock.now = 10.0 * 60.0
    reset = asyncio.create_task(hub._on_meter_change(event(20.0)))  # dramatic drop
    await asyncio.sleep(0)
    clock.now = 11.0 * 60.0
    newer = asyncio.create_task(hub._on_meter_change(event(21.0)))
    await asyncio.sleep(0)
    hub._store.gate.set()
    await reset
    await newer
    assert hub.leak_active is False, "the counter was reset"
    assert any(e[0] == wl.EVENT_LEAK_RESOLVED for e in hub.hass.bus.fired), (
        "the retraction must be announced even when a newer reading landed"
    )


async def test_boot_clears_a_persisted_outage_clock_with_unchanged_value(wl, clock):
    """The _on_available save is the only thing that clears this clock."""
    hub = make_hub()
    clock.now = 2000.0  # after the stored pulse, so it is not discarded
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 900.0,
        "activity": 0.0,
        "unavailable_since": 500.0,
    }
    hub.hass.states.set("sensor.meter", "5000")
    await hub.async_load()
    # the value matches, so the re-anchor returns without saving and no
    # watchdog is armed: _on_available's save is the only thing that clears it
    assert hub.last_pulse_ts == 900.0
    assert hub._timer is None
    assert hub._store.data["unavailable_since"] is None


async def test_signal_lost_stale_token_stands_down(wl, clock):
    """A superseded stale-check callback must not announce a loss."""
    hub = make_hub(stale_min=5)
    await hub.async_load()
    notifications.clear()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    stale_seq = hub._stale_seq
    clock.now = 60.0 + 5.0 * 60.0
    await hub._on_signal_lost(stale_seq - 1)  # superseded token
    assert hub.signal_lost is False
    assert not any(e[0] == wl.EVENT_SIGNAL_LOST for e in hub.hass.bus.fired)
    assert not notifications


async def test_signal_lost_stands_down_when_outage_clock_is_cleared(wl, clock):
    """A cleared clock means the meter is back; do not announce a loss."""
    hub = make_hub(stale_min=5)
    await hub.async_load()
    notifications.clear()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    clock.now = 60.0 + 5.0 * 60.0
    await hub._on_signal_lost(hub._stale_seq)
    assert hub.signal_lost is True
    # the meter returns and the clock is cleared, but a stale callback fires
    await hub._on_meter_change(event(2.0))
    assert hub.unavailable_since is None
    # isolate the guard: without signal_lost and without a clock, only the
    # "the meter is back" term stands between this and a false loss notice
    assert hub.signal_lost is False
    notifications.clear()
    fired_before = len(hub.hass.bus.fired)
    await hub._on_signal_lost(hub._stale_seq)
    assert len(hub.hass.bus.fired) == fired_before
    assert not notifications


async def test_non_positive_readings_do_not_trigger_a_counter_reset(wl, clock, caplog):
    """A dip at or below zero must re-anchor, not reset the counter.

    The ratio test is meaningless for negative values: -2.0 is "more than
    half" of -6.0, so without the gate every reading of a negative meter would
    be classified as a counter reset and detection would never accumulate.
    """
    hub = make_hub()
    await hub.async_load()
    hub.last_value = -6.0
    hub.last_pulse_ts = 0.0
    hub.activity = 40.0
    caplog.clear()
    clock.now = 20.0 * 60.0
    await hub._on_meter_change(event(-8.0))  # a dip: reaches the ratio branch
    assert hub.last_value == -8.0
    assert hub.activity == 40.0, "the re-anchor keeps the evidence"
    assert hub._store.data["last_value"] == -8.0
    assert "assuming counter reset" not in caplog.text
    assert "readback dipped" in caplog.text, "it must be treated as a dip"
    # and the meter keeps accumulating afterwards (the dip kept the pulse
    # clock, so the gap is measured from the last real pulse at t=0)
    clock.now = 40.0 * 60.0
    await hub._on_meter_change(event(-7.0))
    assert hub.activity == 80.0, "40 preserved + 40 real minutes"


async def test_zero_reading_is_not_a_counter_reset(wl, clock, caplog):
    hub = make_hub()
    await hub.async_load()
    hub.last_value = 100.0
    hub.last_pulse_ts = 0.0
    hub.activity = 40.0
    caplog.clear()
    clock.now = 20.0 * 60.0
    await hub._on_meter_change(event(0.0))
    assert hub.last_value == 0.0
    assert hub.activity == 40.0, "re-anchored, evidence preserved"
    assert "assuming counter reset" not in caplog.text


async def test_boot_non_positive_baseline_is_not_a_reset(wl, clock, caplog):
    hub = make_hub()
    clock.now = 20.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": -6.0,
        "last_pulse_ts": 0.0,
        "activity": 40.0,
    }
    hub.hass.states.set("sensor.meter", "-5")
    caplog.clear()
    await hub.async_load()
    assert hub.activity == 40.0
    assert "restarted from" not in caplog.text


async def test_equal_value_recovery_persists_the_reanchored_clock(wl, clock):
    """The re-anchored clock must reach disk, or a restart re-trusts the outage.

    _on_available's save snapshots state before the re-anchor runs, so without
    the recovery save the store keeps the pre-outage pulse time; a later boot
    sees an unchanged value, trusts that stale clock, and charges the whole
    blackout as flow on the next increment.
    """
    hub = make_hub()
    await hub.async_load()
    clock.now = 0.0
    await hub._on_meter_change(event(100.0))
    clock.now = 10.0 * 60.0
    await hub._on_meter_change(event("unavailable"))
    clock.now = 40.0 * 60.0
    await hub._on_meter_change(event(100.0))  # same value, meter recovered
    assert hub.last_pulse_ts == 40.0 * 60.0
    assert hub._store.data["last_pulse_ts"] == 40.0 * 60.0, (
        "the re-anchored clock must be persisted, not just in memory"
    )


async def test_recovery_rearms_a_fresh_quiet_window(wl, clock):
    """A recovery must not inherit the pre-outage watchdog deadline."""
    hub = make_hub(quiet_min=45)
    await hub.async_load()
    clock.now = 0.0
    await hub._on_meter_change(event(100.0))
    hub.activity = 120.0
    hub.leak_active = True
    clock.now = 10.0 * 60.0
    await hub._on_meter_change(event("unavailable"))
    clock.now = 40.0 * 60.0
    await hub._on_meter_change(event(100.0))  # unchanged reading
    # the pre-outage watchdog would fire 5 min from now; the recovery must
    # restart the full window so a still-flowing leak is not declared resolved
    assert timers.handles[-1].delay == 45 * 60


async def test_mid_save_unload_leaves_no_phantom_leak_on_disk(wl, clock):
    hub = make_hub()

    class ShutdownStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.armed = False
            self.done = False

        async def async_save(self, data):
            await super().async_save(data)
            if self.armed and not self.done:
                self.done = True
                self.hub_ref.async_shutdown()

    hub._store = ShutdownStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    hub.last_value = 100.0
    hub.last_pulse_ts = 0.0
    hub.activity = 119.0  # one pulse from the threshold
    hub._store.armed = True
    clock.now = 40.0 * 60.0
    await hub._on_meter_change(event(101.0))
    assert not notifications
    assert hub._store.data["leak_active"] is not True, (
        "an unload must not persist a leak that was never announced"
    )


async def test_discarded_clock_with_unchanged_value_still_reanchors(wl, clock):
    """No usable clock means no trustworthy gap, even when the value matches."""
    hub = make_hub()
    clock.now = 40.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 1e308,  # discarded as a future clock
        "activity": 100.0,
    }
    hub.hass.states.set("sensor.meter", "5000")
    await hub.async_load()
    assert hub.last_pulse_ts == 40.0 * 60.0, "a fresh clock must be anchored"
    assert hub.activity == 100.0, "pre-restart evidence must survive"
    clock.now = 80.0 * 60.0
    await hub._on_meter_change(event(5001.0))
    assert hub.activity == 140.0, "only the real post-boot gap is charged"
    assert hub.leak_active is True


async def test_boot_keeps_evidence_when_a_pulse_landed_during_downtime(wl, clock):
    """Stale by the stored clock, but a pulse arrived: the evidence stands.

    Both terms of the reset guard matter: the gap exceeded quiet_min (stale),
    yet the live value proves a pulse happened while we were down, so the
    accumulated activity must survive and a real leak must still be able to fire.
    """
    hub = make_hub()
    clock.now = 100.0 * 60.0  # far past quiet_min
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "activity": 119.0,
    }
    hub.hass.states.set("sensor.meter", "5002")  # moved while we were down
    await hub.async_load()
    assert hub.activity == 119.0, "a pulse during downtime keeps the evidence"
    clock.now = 140.0 * 60.0
    await hub._on_meter_change(event(5003.0))
    assert hub.activity == 159.0
    assert hub.leak_active is True, "a real near-threshold leak must still fire"


async def test_boot_stale_activity_is_reset_when_nothing_moved(wl, clock):
    """The same store, but the meter did not report: the activity is stale."""
    hub = make_hub()
    clock.now = 100.0 * 60.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 5000.0,
        "last_pulse_ts": 0.0,
        "activity": 119.0,
    }
    hub.hass.states.set("sensor.meter", "5000")  # unchanged: nothing was missed
    await hub.async_load()
    assert hub.activity == 0.0, "quiet since the last pulse: stale"
    assert hub._store.data["activity"] == 0.0


async def test_already_lost_is_not_announced_again_at_boot(wl, clock):
    """A boot re-arms a zero-delay check; it must not re-announce the loss."""
    hub = make_hub(stale_min=5)
    clock.now = 5000.0
    hub._store.data = {
        "water_meter": "sensor.meter",
        "last_value": 100.0,
        "signal_lost": True,
        "unavailable_since": 100.0,  # long past the deadline
    }
    hub.hass.states.set("sensor.meter", "unavailable")
    await hub.async_load()
    assert timers.handles[-1].delay == 0, "the deadline has passed: fires at once"
    await timers.fire_last()
    assert hub.signal_lost is True
    assert not any(e[0] == wl.EVENT_SIGNAL_LOST for e in hub.hass.bus.fired), (
        "the loss was already announced before the restart"
    )
    assert hub.hass.services.calls == [], "no duplicate notification may go out"


async def test_no_alert_after_shutdown(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    hub.async_shutdown()
    notifications.clear()
    fired_before = len(hub.hass.bus.fired)
    assert await hub._simulate_leak() is False  # already leaking, but also stopped
    clock.now = 165.0 * 60.0
    await hub._on_quiet(hub._watchdog_seq)  # an in-flight callback after unload
    assert not notifications
    assert len(hub.hass.bus.fired) == fired_before
    assert hub.leak_active is True, "a stopped hub must not run transitions"


async def test_simulate_leak_suppressed_after_shutdown(wl):
    hub = make_hub()
    await hub.async_load()
    hub.async_shutdown()
    notifications.clear()
    assert await hub._simulate_leak() is False
    assert not notifications


async def test_shutdown_during_quiet_resolve_stops_further_effects(wl, clock):
    """Unloading while the resolve-save is awaited must stop the callback."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False
            self.prev_leak = False

        async def async_save(self, data):
            if self.prev_leak and not data.get("leak_active") and not self.injected:
                self.injected = True
                self.hub_ref.async_shutdown()
            self.prev_leak = bool(data.get("leak_active"))
            await super().async_save(data)

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    fired_before = len(hub.hass.bus.fired)
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert not notifications
    assert len(hub.hass.bus.fired) == fired_before


async def test_stopped_hub_suppresses_every_outbound_path(wl, clock):
    """Every outbound side effect is gated on the hub not being unloaded."""
    hub = make_hub()
    await hub.async_load()
    hub.async_shutdown()
    notifications.clear()
    fired_before = len(hub.hass.bus.fired)
    writes: list[int] = []
    hub._entities = [
        type(
            "E",
            (),
            {
                "update_from_hub": lambda s: None,
                "async_write_ha_state": lambda s: writes.append(1),
            },
        )()
    ]

    await hub._send_alert()
    await hub._send_resolved()
    await hub._send_signal_lost()
    await hub._send_signal_restored()
    await hub._notify("t", "m", "water_leak_test")
    await hub._on_meter_change(event(1.0))
    await hub._on_unavailable(1.0)
    await hub._on_available()
    await hub._on_signal_lost(0)
    await hub._on_quiet(0)
    hub._notify_entities()
    hub.add_entity(hub._entities[0])

    assert not notifications
    assert hub.hass.services.calls == [], (
        "a stopped hub must not call out to a real notify provider"
    )
    assert len(hub.hass.bus.fired) == fired_before
    assert writes == []
    assert hub.last_value is None and hub.unavailable_since is None
    assert hub.leak_active is False and hub.signal_lost is False


# --- tracking -------------------------------------------------------------

async def test_start_tracking_subscribes(wl, clock):
    hub = make_hub()
    hub.async_start_tracking()
    assert tracker.calls == [(hub.hass, ("sensor.meter",), hub._on_meter_change)]
    handler = tracker.last_handler()
    clock.now = 30.0 * 60.0
    await handler(event(1.0))
    assert hub.last_value == 1.0
    assert hub.last_pulse_ts == 30.0 * 60.0


# --- pulse cadence / leak detection -----------------------------------------


async def test_pulse_non_number_triggers_unavailable(wl, clock):
    hub = make_hub()
    await hub.async_load()
    clock.now = 30.0
    await hub._on_meter_change(event("unavailable"))
    assert hub.unavailable_since == 30.0
    assert hub._stale_timer is not None
    # the outage clock is persisted so a mid-outage restart re-arms the stale
    # check against the real deadline instead of starting a fresh one
    assert hub._store.data["unavailable_since"] == 30.0


async def test_pulse_equal_value_repeat_ignored(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await hub._on_meter_change(event(10.0))
    clock.now = 60.0
    await hub._on_meter_change(event(10.0))  # meter repeats the same read
    assert hub.last_pulse_ts == 0.0  # not advanced by the repeat
    assert hub.activity == 0.0


async def test_short_dropout_clears_persisted_clock(wl, clock):
    hub = make_hub()
    await hub.async_load()
    clock.now = 0.0
    await hub._on_meter_change(event(100.0))
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))  # arms + persists the clock
    assert hub._store.data["unavailable_since"] == 60.0
    # meter recovers via an equal-value repeat before the stale deadline:
    # the cleared clock must reach the store, or a restart before the next
    # pulse save would reload the stale start time and corrupt tracking
    clock.now = 120.0
    await hub._on_meter_change(event(100.0))
    assert hub.unavailable_since is None
    assert hub._store.data["unavailable_since"] is None


async def test_outage_gap_is_not_counted_as_flow(wl, clock):
    """An RF dropout is unknown time, not evidence of continuous flow."""
    hub = make_hub()
    await hub.async_load()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    hub.activity = 100.0
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    # meter returns 30 min later: the 31 min gap spans the blackout
    clock.now = 31.0 * 60.0
    await hub._on_meter_change(event(2.0))
    assert hub.activity == 100.0, "blackout time must not accumulate as flow"
    assert hub.leak_active is False
    assert not any(e[0] == wl.EVENT_LEAK_DETECTED for e in hub.hass.bus.fired)


async def test_outage_while_leak_active_does_not_resolve_on_quiet(wl, clock):
    """A leak seen before an outage must not be 'resolved' by the blackout."""
    hub = make_hub(quiet_min=5, limit_min=15)
    await hub.async_load()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    hub.activity = 15.0
    hub.leak_active = True
    notifications.clear()
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    clock.now = 6.0 * 60.0
    # the quiet watchdog is the first handle; the stale check was armed after
    await timers.fire(timers.handles[0])
    assert hub.leak_active is True, "silence without signal proves nothing"
    assert hub.activity == 15.0, (
        "the pre-outage evidence must stand; without the dark re-arm the "
        "activity is zeroed while the leak sensor stays on"
    )
    assert not any(e[0] == wl.EVENT_LEAK_RESOLVED for e in hub.hass.bus.fired)
    assert not notifications
    assert hub._timer is not None, "the watchdog must be re-armed, not dropped"


async def test_outage_recovery_reanchors_pulse_clock_on_equal_value(wl, clock):
    """A same-value recovery must not leave the blackout to be charged as flow."""
    hub = make_hub()
    await hub.async_load()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    clock.now = 31.0 * 60.0
    await hub._on_meter_change(event(1.0))  # same value, meter recovered
    assert hub.last_pulse_ts == 31.0 * 60.0
    # the very next increment is one minute of real flow, not 32
    clock.now = 32.0 * 60.0
    await hub._on_meter_change(event(2.0))
    assert hub.activity == 1.0


async def test_pulse_small_dip_reanchors_value(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await hub._on_meter_change(event(10.0))
    hub.activity = 40.0
    clock.now = 60.0
    await hub._on_meter_change(event(5.0))  # small dip: reading noise
    assert hub.last_value == 5.0  # re-anchored, so later reads are accepted
    assert hub.last_pulse_ts == 0.0  # pulse clock untouched
    assert hub.activity == 40.0  # a jitter glitch cannot cancel a real leak
    assert hub._store.data["last_value"] == 5.0


async def test_in_band_meter_swap_recovers_detection(wl, clock):
    hub = make_hub()
    await hub.async_load()
    hub.last_value = 500.0
    hub.last_pulse_ts = 0.0
    clock.now = 240.0 * 60.0
    await hub._on_meter_change(event(250.0))  # pre-used replacement: no 2x drop
    assert hub.last_value == 250.0
    clock.now = 240.0 * 60.0 + 40.0 * 60.0
    await hub._on_meter_change(event(251.0))  # gap >= quiet -> activity reset
    assert hub.activity == 0.0
    clock.now = (240.0 + 80.0) * 60.0
    await hub._on_meter_change(event(252.0))  # 40 min gap < 45 -> accumulates
    assert hub.last_value == 252.0
    assert hub.activity == 40.0  # detection is alive again after the swap


async def test_pulse_counter_reset_clears_active_leak(wl, clock):
    hub = make_hub()
    await hub.async_load()
    notifications.clear()
    hub.last_value = 5000.0
    hub.last_pulse_ts = 0.0
    hub.activity = 120.0
    hub.leak_active = True
    clock.now = 130.0 * 60.0
    await hub._on_meter_change(event(3.0))  # dramatic drop mid-leak
    assert hub.leak_active is False  # a replaced counter voids the old flag
    assert hub.activity == 0.0
    assert hub._store.data["leak_active"] is False
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_RESOLVED, None)
    # the notice must say why, not falsely claim a quiet interval elapsed
    message = last_notify_data(hub)["message"].lower()
    assert "counter on" in message
    assert "basement" not in message  # no live state here, so the id is used
    assert "no water flow" not in message
    assert last_notify_data(hub)["title"] == "♻️ Water leak resolved"
    assert hub._timer is not None  # the reset re-arms the watchdog on the new counter


async def test_pulse_counter_reset_rebaselines(wl, clock):
    hub = make_hub()
    await hub.async_load()
    hub.last_value = 1000.0
    hub.last_pulse_ts = 0.0
    hub.activity = 119.0  # one minute under the 120 limit
    # A dramatic drop means the meter was replaced / reset: re-baseline so
    # detection does not stay dead for every subsequent reading.
    clock.now = 120.0 * 60.0
    await hub._on_meter_change(event(3.0))
    assert hub.last_value == 3.0
    assert hub.last_pulse_ts == 120.0 * 60.0
    assert hub.activity == 0.0  # accumulation starts fresh on the new baseline
    assert hub._store.data["last_value"] == 3.0
    # the new series can accumulate and fire a leak again
    clock.now = 160.0 * 60.0  # 40 min gap < 45 quiet_min
    await hub._on_meter_change(event(4.0))
    assert hub.activity == 40.0


async def test_first_pulse_starts_fresh(wl, clock):
    hub = make_hub()
    await hub.async_load()
    clock.now = 30.0 * 60.0
    await hub._on_meter_change(event(1.0))
    assert hub.last_value == 1.0
    assert hub.last_pulse_ts == 30.0 * 60.0
    assert hub.activity == 0.0
    assert len(timers) == 1
    assert hub._timer is not None
    assert hub._store.data["last_value"] == 1.0


async def test_rapid_pulses_accumulate_activity(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0)])
    assert hub.activity == 80.0
    assert hub.leak_active is False  # 80 < 120 limit


async def test_slow_gap_resets_activity(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await pump(hub, clock, [(0, 1.0), (50, 2.0)])  # 50 > 45 quiet
    assert hub.activity == 0.0


async def test_leak_fires_when_limit_crossed(wl, clock):
    hub = make_hub()
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    assert hub._store.data["leak_active"] is True
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_DETECTED, {"activity_min": 120.0})
    assert last_notify_data(hub)["title"] == "💧 Water leak detected"
    assert "120" in last_notify_data(hub)["message"]


async def test_suppressed_leak_does_not_fire(wl, clock):
    hub = make_hub()
    hub.suppressed = True
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is False
    assert not notifications


# --- watchdog / quiet handling ----------------------------------------------

async def test_watchdog_resolves_normal_leak(wl, clock):
    hub = make_hub()
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert hub.leak_active is False
    assert hub.activity == 0.0
    assert hub._timer is None
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_RESOLVED, None)
    assert last_notify_data(hub)["title"] == "✅ Water leak resolved"


async def test_watchdog_with_no_leak_just_resets_activity(wl, clock):
    hub = make_hub()
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0)])
    assert hub.activity == 0.0
    assert hub.leak_active is False
    clock.now = 46.0 * 60.0
    await timers.fire_last()
    assert hub.activity == 0.0
    assert not notifications  # nothing was leaking to resolve


async def test_watchdog_no_leak_persists_activity_reset(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0)])
    assert hub.activity == 80.0
    assert hub._store.data["activity"] == 80.0
    clock.now = 126.0 * 60.0
    await timers.fire_last()
    assert hub.activity == 0.0
    assert hub.leak_active is False
    assert hub._store.data["activity"] == 0.0  # persisted, no phantom restart


async def test_watchdog_stale_token_stands_down(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await pump(hub, clock, [(0, 1.0), (5, 2.0)])  # two watchdogs scheduled
    hub.leak_active = True
    notifications.clear()
    clock.now = 46.0 * 60.0
    await hub._on_quiet(0)  # a stale token from a superseded watchdog
    assert hub.leak_active is True  # must not clear a fresh watchdog's scope
    assert not notifications


async def test_watchdog_resolve_with_pulse_during_save(wl, clock):
    """A pulse that does not re-trigger the leak must still emit 'resolved'."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False
            self.prev_leak = False

        async def async_save(self, data):
            if self.prev_leak and not data.get("leak_active") and not self.injected:
                self.injected = True
                await self.hub_ref._on_meter_change(event(self.hub_ref.last_value + 1))
            self.prev_leak = bool(data.get("leak_active"))
            await super().async_save(data)

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    before = len(hub.hass.services.calls)
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    # the injected pulse (gap == quiet_min) resets activity below the limit, so
    # the leak genuinely resolved: the resolved notice must NOT be swallowed
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_RESOLVED, None)
    assert len(hub.hass.services.calls) == before + 1, "the 'resolved' notice must fire"
    assert hub.leak_active is False
    assert hub._timer is not None, "the injected pulse must re-arm the watchdog"


async def test_watchdog_resolve_suppressed_when_leak_retriggered_during_save(wl, clock):
    """A (re)triggered leak during the resolve-save must never be followed
    by a spurious 'resolved'. Guards the suppression invariant itself (both the
    old token check and the state check satisfy it); the two tests above
    distinguish the state check by proving 'resolved' DOES fire when no
    re-trigger happens mid-save."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False
            self.prev_leak = False

        async def async_save(self, data):
            if self.prev_leak and not data.get("leak_active") and not self.injected:
                self.injected = True
                await self.hub_ref._simulate_leak()
            self.prev_leak = bool(data.get("leak_active"))
            await super().async_save(data)

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    assert hub.leak_active is True
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert hub.leak_active is True  # simulate re-raised the leak mid-resolve
    assert wl.EVENT_LEAK_RESOLVED not in [e[0] for e in hub.hass.bus.fired]
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_DETECTED, {"activity_min": 135.0})


async def test_watchdog_resolve_with_counter_reset_during_save(wl, clock):
    """A dramatic drop landing mid-resolve must not swallow the 'resolved'."""
    hub = make_hub()

    class InjectingStore(StoreStub):
        def __init__(self, hass, version, key, hub_ref):
            super().__init__(hass, version, key)
            self.hub_ref = hub_ref
            self.injected = False
            self.prev_leak = False

        async def async_save(self, data):
            if self.prev_leak and not data.get("leak_active") and not self.injected:
                self.injected = True
                await self.hub_ref._on_meter_change(event(3.0))
            self.prev_leak = bool(data.get("leak_active"))
            await super().async_save(data)

    hub._store = InjectingStore(hub.hass, 1, "water_leak_meter.e1", hub)
    await hub.async_load()
    notifications.clear()
    await pump(hub, clock, [(0, 1.0), (40, 2.0), (80, 3.0), (120, 4.0)])
    hub.last_value = 5000.0  # the injected 3.0 is a dramatic drop at resolve time
    assert hub.leak_active is True
    before = len(hub.hass.services.calls)
    clock.now = 165.0 * 60.0
    await timers.fire_last()
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_RESOLVED, None)
    assert len(hub.hass.services.calls) == before + 1, "the 'resolved' notice must fire"
    assert hub.leak_active is False
    assert hub.activity == 0.0


# --- signal-loss handling ---------------------------------------------------

async def test_signal_loss_alert_and_recovery(wl, clock):
    hub = make_hub()
    await hub.async_load()
    notifications.clear()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))

    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    assert hub.unavailable_since == 60.0
    assert not notifications

    clock.now = 60.0 + 180.0 * 60.0
    await timers.fire_last()
    assert hub.signal_lost is True
    assert hub._store.data["signal_lost"] is True
    assert hub._store.data["unavailable_since"] == 60.0
    assert hub.hass.bus.fired[-1] == (
        wl.EVENT_SIGNAL_LOST,
        {"water_meter": "sensor.meter", "stale_min": 180},
    )
    assert last_notify_data(hub)["title"] == "📡 Water meter signal lost"

    clock.now = 60.0 + 180.0 * 60.0 + 60.0
    await hub._on_meter_change(event(2.0))
    assert hub.signal_lost is False
    assert hub.unavailable_since is None
    assert hub._stale_timer is None
    assert hub.hass.bus.fired[-1] == (wl.EVENT_SIGNAL_RESTORED, None)
    assert last_notify_data(hub)["title"] == "📶 Water meter signal restored"


async def test_signal_loss_disabled_no_timer(wl, clock):
    hub = make_hub(stale_min=0)
    await hub.async_load()
    notifications.clear()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    n_timers = len(timers)
    clock.now = 60000.0
    await hub._on_meter_change(event("unavailable"))
    assert len(timers) == n_timers  # nothing armed when stale_min is 0
    assert not notifications
    await hub._on_signal_lost(0)  # token 0 == current seq, hits the guard below
    assert hub.signal_lost is False  # disabled check exits immediately


async def test_signal_check_early_exits(wl, clock):
    """The stale check must stand down on stale token / already-lost / too soon."""
    hub = make_hub(stale_min=5)
    await hub.async_load()
    notifications.clear()
    clock.now = 0.0
    await hub._on_meter_change(event(1.0))
    clock.now = 60.0
    await hub._on_meter_change(event("unavailable"))
    # stale token: nothing happens
    await hub._on_signal_lost(99_999)
    assert hub.signal_lost is False
    # too early: 1 min elapsed < 5 min threshold -> reschedules the tail
    clock.now = 60.0 + 60.0  # 1 min into the outage
    await timers.fire_last()
    assert hub.signal_lost is False
    assert hub._stale_timer is not None, "too-early check must reschedule"
    assert timers.handles[-1].delay == 4 * 60
    assert not notifications


async def test_signal_already_lost_or_unset_returns(wl, clock):
    hub = make_hub(stale_min=5)
    await hub.async_load()
    # no unavailable window at all -> returns without firing
    await hub._on_signal_lost(0)  # token 0 == current seq
    assert hub.signal_lost is False
    # already lost -> returns without re-firing
    hub.signal_lost = True
    await hub._on_signal_lost(0)
    assert hub.signal_lost is True


async def test_unavailable_twice_keeps_one_clock(wl, clock):
    hub = make_hub()
    await hub.async_load()
    clock.now = 10.0
    await hub._on_unavailable(10.0)
    first_timer = timers.last()
    clock.now = 20.0
    await hub._on_unavailable(20.0)
    assert hub.unavailable_since == 10.0
    assert first_timer.cancelled is False
    assert hub._stale_timer is not None
    # re-arming replaces the previous handle (cancelling it)
    hub._schedule_stale_check()
    replaced = timers.last()
    hub._schedule_stale_check()
    assert replaced.cancelled is True
    assert len(timers) == 3


# --- notify paths -----------------------------------------------------------

async def test_notify_disabled_no_call(wl):
    hub = make_hub(notify_service="")
    await hub._notify("Water leak detected", "test", "water_leak_alert")
    assert hub.hass.services.calls == []


async def test_notify_unknown_domain_warns(wl, caplog):
    hub = make_hub(notify_service="nonsense.invalid")
    await hub._notify("Water leak detected", "test", "water_leak_alert")
    assert hub.hass.services.calls == []
    assert "Invalid notify service" in caplog.text


async def test_notify_calls_service(wl):
    hub = make_hub(notify_service="notify.telegram")
    await hub._notify("Water leak detected", "msg", "water_leak_alert")
    assert hub.hass.services.calls == [
        ("notify", "telegram", {"title": "Water leak detected", "message": "msg"})
    ]


async def test_notify_merges_data_into_payload(wl):
    hub = make_hub(
        notify_service="notify.telegram", notify_data='{"chat_id": 7, "tag": "leak"}'
    )
    await hub._notify("Water leak detected", "msg", "water_leak_alert")
    assert hub.hass.services.calls == [
        (
            "notify",
            "telegram",
            {
                "title": "Water leak detected",
                "message": "msg",
                "data": {"chat_id": 7, "tag": "leak"},
            },
        )
    ]


async def test_notify_telegram_bot_merges_at_top_level(wl):
    hub = make_hub(
        notify_service="telegram_bot.send_message",
        notify_data='{"chat_id": 7, "parse_mode": "markdown"}',
    )
    await hub._notify("Water leak detected", "msg", "water_leak_alert")
    assert hub.hass.services.calls == [
        (
            "telegram_bot",
            "send_message",
            {
                "title": "Water leak detected",
                "message": "msg",
                "chat_id": 7,
                "parse_mode": "markdown",
            },
        )
    ]


async def test_notify_failure_falls_back_to_persistent(wl):
    hub = make_hub(notify_service="notify.telegram")

    class FailingServices(FakeServices):
        async def async_call(self, domain, service, service_data, **kwargs):
            self.blocking_calls.append(kwargs.get("blocking", False))
            raise RuntimeError("boom")

    hub.hass.services = FailingServices({"notify": {"telegram": True}})
    notifications.clear()
    await hub._notify("Water leak resolved", "stopped", "water_leak_resolved")
    assert ("stopped", "Water leak resolved", "water_leak_resolved") in notifications
    # the fallback must come from the provider failure, not a TypeError on the
    # stub's signature
    assert hub.hass.services.blocking_calls == [True]


async def test_notify_timeout_falls_back_to_persistent(wl, monkeypatch):
    """A hung service call must be abandoned by the wait_for timeout itself."""
    hub = make_hub(notify_service="notify.telegram")
    started = asyncio.Event()

    class HangingServices(FakeServices):
        async def async_call(self, domain, service, service_data, **kwargs):
            started.set()
            await asyncio.Event().wait()  # never completes

    real_wait_for = asyncio.wait_for
    seen: list[float | None] = []

    async def fake_wait_for(awaitable, timeout):
        seen.append(timeout)
        return await real_wait_for(awaitable, timeout=0.01)

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    hub.hass.services = HangingServices({"notify": {"telegram": True}})
    notifications.clear()
    await hub._notify("Water leak detected", "msg", "water_leak_alert")
    assert started.is_set()  # the call really was started before timing out
    assert seen == [10], "the 10s timeout must be applied by wait_for"
    assert ("msg", "Water leak detected", "water_leak_alert") in notifications


# --- leak simulation ---------------------------------------------------------

async def test_simulate_leak_default_gap(wl):
    hub = make_hub(quiet_min=5, limit_min=15)
    notifications.clear()
    fired = await hub._simulate_leak()
    assert fired is True
    assert hub.leak_active is True
    assert hub.activity == 15.0  # gap 2.5 min * ceil(15/2.5) = 15.0
    assert hub.hass.bus.fired[-1] == (wl.EVENT_LEAK_DETECTED, {"activity_min": 15.0})
    assert last_notify_data(hub)["title"] == "💧 Water leak detected"
    assert len(timers) == 1  # one quiet watchdog
    assert hub._timer is not None


async def test_simulate_leak_custom_gap(wl):
    hub = make_hub(quiet_min=5, limit_min=15)
    fired = await hub._simulate_leak(gap_min=10.0)
    assert fired is True
    assert hub.activity == 20.0  # 10 * ceil(15/10)
    assert hub.leak_active is True


async def test_simulate_leak_does_not_touch_real_pulse(wl, clock):
    hub = make_hub(quiet_min=5, limit_min=15)
    hub.last_value = 100.0
    hub.last_pulse_ts = clock.now - 45.0
    await hub._simulate_leak()
    assert hub.last_value == 100.0
    assert hub.last_pulse_ts == clock.now - 45.0


async def test_simulate_leak_refuses_when_active_or_suppressed(wl):
    hub = make_hub(quiet_min=5, limit_min=15)
    hub.leak_active = True
    assert await hub._simulate_leak() is False
    hub.leak_active = False
    hub.suppressed = True
    assert await hub._simulate_leak() is False


async def test_simulate_leak_guards_invalid_gap(wl):
    hub = make_hub(quiet_min=5, limit_min=15)
    assert await hub._simulate_leak(gap_min=0.0) is False
    assert await hub._simulate_leak(gap_min=-5.0) is False
    assert await hub._simulate_leak() is True
    assert hub.leak_active is True
    hub.leak_active = False
    hub.limit_min = 0
    assert await hub._simulate_leak() is False


async def test_simulate_leak_save_failure_logged_not_fatal(wl, caplog):
    hub = make_hub(quiet_min=5, limit_min=15)

    class FailingStore(StoreStub):
        async def async_save(self, data):
            raise RuntimeError("disk full")

    hub._store = FailingStore(hub.hass, 1, "water_leak_meter.e1")
    assert await hub._simulate_leak() is True
    assert "could not save state" in caplog.text


async def test_simulate_leak_alert_failure_logged_not_fatal(wl, monkeypatch, caplog):
    hub = make_hub(quiet_min=5, limit_min=15)

    async def boom(self, title, message, notification_id):
        raise RuntimeError("notify down")

    monkeypatch.setattr(wl.WaterLeakHub, "_notify", boom)
    assert await hub._simulate_leak() is True
    assert "could not send alert" in caplog.text


# --- suppressed switch, entity fan-out, shutdown -----------------------------

async def test_set_suppressed_notifies_entities(wl):
    hub = make_hub()
    writes = []
    hub._entities = [NS(update_from_hub=lambda: writes.append("u"), async_write_ha_state=lambda: writes.append("w"))]
    hub.set_suppressed(True)
    assert hub.suppressed is True
    assert writes == ["u", "w"]
    hub.set_suppressed(False)
    assert hub.suppressed is False
    assert len(writes) == 4


async def test_add_entity_registers_and_writes(wl):
    hub = make_hub()
    calls = []
    entity = NS(
        update_from_hub=lambda: calls.append("update"),
        async_write_ha_state=lambda: calls.append("write"),
    )
    hub.add_entity(entity)
    assert hub._entities == [entity]
    assert calls == ["update", "write"]
    calls.clear()
    hub._notify_entities()
    assert calls == ["update", "write"]


async def test_async_shutdown_without_tracking(wl):
    hub = make_hub()
    await hub.async_load()
    hub._schedule_stale_check()  # arms a stale timer without tracking
    hub.async_shutdown()
    assert tracker.unsubscribed == 0
    assert timers.last().cancelled is True


async def test_available_cancels_pending_stale_check(wl, clock):
    hub = make_hub()
    await hub.async_load()
    clock.now = 10.0
    await hub._on_unavailable(10.0)  # arms the stale check
    pending = timers.last()
    await hub._on_available()  # a pulse lands before the timeout
    assert pending.cancelled is True
    assert hub._stale_timer is None


async def test_simulate_leak_clamps_minimum_activity(wl, monkeypatch):
    hub = make_hub(quiet_min=5, limit_min=15)
    monkeypatch.setattr(wl.math, "ceil", lambda x: 0)
    assert await hub._simulate_leak() is True
    assert hub.activity == 15.0  # floored to the limit


async def test_async_shutdown_cancels_everything(wl, clock):
    hub = make_hub()
    await hub.async_load()
    hub.async_start_tracking()
    await pump(hub, clock, [(0, 1.0)])  # arms the quiet watchdog
    clock.now = 5.0 * 60.0
    await hub._on_meter_change(event("unavailable"))  # arms the stale check
    watchdog = timers.handles[0]
    stale = timers.handles[-1]
    assert hub._timer is not None and hub._stale_timer is not None
    hub.async_shutdown()
    assert tracker.unsubscribed == 1
    assert watchdog.cancelled is True
    assert stale.cancelled is True


async def test_send_signal_lost_and_restored_events(wl):
    hub = make_hub(stale_min=5, notify_service="")
    notifications.clear()
    await hub._send_signal_lost()
    assert hub.hass.bus.fired[-1] == (
        wl.EVENT_SIGNAL_LOST,
        {"water_meter": "sensor.meter", "stale_min": 5},
    )
    await hub._send_signal_restored()
    assert hub.hass.bus.fired[-1] == (wl.EVENT_SIGNAL_RESTORED, None)


async def test_resolved_and_signal_messages_are_named(wl):
    hub = make_hub()
    await hub._send_alert()
    assert last_notify_data(hub)["title"] == "💧 Water leak detected"
    await hub._send_resolved()
    assert last_notify_data(hub)["title"] == "✅ Water leak resolved"
    await hub._send_signal_lost()
    assert "signal lost" in last_notify_data(hub)["title"].lower()