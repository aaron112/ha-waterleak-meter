"""Unit tests for the WaterLeakHub detection logic."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from conftest import FakeBus, FakeHass, FakeServices, StoreStub, make_hub, notifications, timers, tracker


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


async def test_async_load_persisted_state(wl):
    hub = make_hub()
    hub._store.data = {
        "last_value": 100.0,
        "last_pulse_ts": 123.0,
        "activity": 55.0,
        "leak_active": True,
        "signal_lost": True,
        "unavailable_since": 42.0,
    }
    await hub.async_load()
    assert hub.last_value == 100.0
    assert hub.last_pulse_ts == 123.0
    assert hub.activity == 55.0
    assert hub.signal_lost is True
    assert hub.unavailable_since == 42.0


async def test_async_load_persisted_leak_arms_watchdog(wl):
    hub = make_hub()
    hub._store.data = {"leak_active": True, "activity": 10.0}
    assert hub._timer is None
    await hub.async_load()
    assert hub.leak_active is True
    assert hub._timer is not None, "boot must arm the quiet watchdog for a persisted leak"
    await timers.fire_last()
    assert hub.leak_active is False


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
    assert last_notify_data(hub)["title"] == "Water meter signal restored"


async def test_check_initial_state_offline_rearms_clock(wl, clock):
    hass = FakeHass()
    hass.states.set("sensor.meter", "unavailable")
    clock.now = 100.0
    hub = make_hub(hass=hass)
    await hub.async_load()
    assert hub.unavailable_since == 100.0
    assert hub._stale_timer is not None


async def test_check_initial_state_offline_keeps_existing_clock(wl, clock):
    hass = FakeHass()
    hass.states.set("sensor.meter", "unavailable")
    clock.now = 500.0
    hub = make_hub(hass=hass)
    hub._store.data = {"unavailable_since": 100.0}
    await hub.async_load()
    assert hub.unavailable_since == 100.0  # preserved, not reset
    assert hub._stale_timer is None  # no second clock armed


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

async def test_pulse_new_state_none_is_ignored(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await hub._on_meter_change(empty_event())
    assert hub.last_value is None
    assert hub.last_pulse_ts is None


async def test_pulse_non_number_triggers_unavailable(wl, clock):
    hub = make_hub()
    await hub.async_load()
    clock.now = 30.0
    await hub._on_meter_change(event("unavailable"))
    assert hub.unavailable_since == 30.0
    assert hub._stale_timer is not None


async def test_pulse_non_increasing_ignored(wl, clock):
    hub = make_hub()
    await hub.async_load()
    await hub._on_meter_change(event(10.0))
    before_last = hub.last_value
    before_ts = hub.last_pulse_ts
    clock.now = 60.0
    await hub._on_meter_change(event(5.0))  # meter never goes backwards
    assert hub.last_value == before_last
    assert hub.last_pulse_ts == before_ts


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
    assert last_notify_data(hub)["title"] == "Water leak detected"
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
    assert last_notify_data(hub)["title"] == "Water leak resolved"


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
    """A pulse landing during the resolve-save must not emit 'resolved'."""
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
    assert hub.hass.bus.fired[-1][0] != wl.EVENT_LEAK_RESOLVED
    assert len(hub.hass.services.calls) == before, "spurious 'resolved' must not fire"
    assert hub.leak_active is False
    assert hub._timer is not None, "the injected pulse must re-arm the watchdog"


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
    assert last_notify_data(hub)["title"] == "Water meter signal lost"

    clock.now = 60.0 + 180.0 * 60.0 + 60.0
    await hub._on_meter_change(event(2.0))
    assert hub.signal_lost is False
    assert hub.unavailable_since is None
    assert hub._stale_timer is None
    assert hub.hass.bus.fired[-1] == (wl.EVENT_SIGNAL_RESTORED, None)
    assert last_notify_data(hub)["title"] == "Water meter signal restored"


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
    await hub._on_signal_lost(1)
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
    # too early: 1 min elapsed < 5 min threshold
    await timers.fire_last()
    assert hub.signal_lost is False
    assert not notifications


async def test_signal_already_lost_or_unset_returns(wl, clock):
    hub = make_hub(stale_min=5)
    await hub.async_load()
    # no unavailable window at all -> returns without firing
    await hub._on_signal_lost(hub._stale_seq if hub._stale_seq else 1)
    assert hub.signal_lost is False
    # already lost -> returns without re-firing
    hub.signal_lost = True
    await hub._on_signal_lost(1)
    assert hub.signal_lost is True


async def test_unavailable_twice_keeps_one_clock(wl, clock):
    hub = make_hub()
    await hub.async_load()
    clock.now = 10.0
    hub._on_unavailable(10.0)
    first_timer = timers.last()
    clock.now = 20.0
    hub._on_unavailable(20.0)
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
        async def async_call(self, domain, service, service_data):
            raise RuntimeError("boom")

    hub.hass.services = FailingServices({"notify": {"telegram": True}})
    notifications.clear()
    await hub._notify("Water leak resolved", "stopped", "water_leak_resolved")
    assert ("stopped", "Water leak resolved", "water_leak_resolved") in notifications


async def test_notify_timeout_falls_back_to_persistent(wl):
    hub = make_hub(notify_service="notify.telegram")

    class HangingServices(FakeServices):
        async def async_call(self, domain, service, service_data):
            raise asyncio.TimeoutError

    hub.hass.services = HangingServices({"notify": {"telegram": True}})
    notifications.clear()
    await hub._notify("Water leak detected", "msg", "water_leak_alert")
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
    assert last_notify_data(hub)["title"] == "Water leak detected"
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
    hub._on_unavailable(10.0)  # arms the stale check
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
    assert last_notify_data(hub)["title"] == "Water leak detected"
    await hub._send_resolved()
    assert last_notify_data(hub)["title"] == "Water leak resolved"
    await hub._send_signal_lost()
    assert "signal lost" in last_notify_data(hub)["title"].lower()