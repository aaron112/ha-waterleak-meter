"""Unit tests for async_setup_entry / async_unload_entry / the update listener."""

from conftest import FakeHass, make_entry, tracker

import custom_components.water_leak_meter as wl
from custom_components.water_leak_meter import DOMAIN, PLATFORMS, WaterLeakHub


async def test_async_setup_entry_initializes_hub():
    hass = FakeHass()
    entry = make_entry(notify_service="notify.telegram", quiet_min="30")
    assert await wl.async_setup_entry(hass, entry) is True
    hub = hass.data[DOMAIN]["e1"]
    assert isinstance(hub, WaterLeakHub)
    assert hub.water_meter == "sensor.meter"
    assert hub.quiet_min == 30
    assert len(tracker.calls) == 1
    assert hass.config_entries.forwarded == [(entry, PLATFORMS)]
    assert len(entry.listeners) == 1
    assert entry.on_unload  # the update listener is registered for cleanup


async def test_setup_reconciles_a_meter_change_during_startup(monkeypatch):
    """A reading that lands mid-setup must be adopted, not lost.

    The post-subscribe reconcile exists for exactly this: the meter moved in
    the window between the load-time reconciliation and the moment events
    start being observed, so nothing else would ever see the value.
    """
    hass = FakeHass()
    entry = make_entry()
    original = wl.async_track_state_change_event

    def track_and_drift(*args, **kwargs):
        # the meter reports while we are between the two reconciles
        hass.states.set("sensor.meter", "5005")
        return original(*args, **kwargs)

    monkeypatch.setattr(wl, "async_track_state_change_event", track_and_drift)
    assert await wl.async_setup_entry(hass, entry) is True
    hub = hass.data[DOMAIN]["e1"]
    assert hub.last_value == 5005.0, "the drifted value must be adopted"


async def test_async_unload_entry_tears_down_hub():
    hass = FakeHass()
    entry = make_entry()
    await wl.async_setup_entry(hass, entry)
    assert tracker.unsubscribed == 0
    assert await wl.async_unload_entry(hass, entry) is True
    assert hass.data[DOMAIN] == {}  # entry popped; the domain dict remains
    assert hass.config_entries.unloaded == [(entry, PLATFORMS)]
    assert tracker.unsubscribed == 1
    for cb in entry.on_unload:
        cb()


async def test_async_update_listener_reloads_entry():
    hass = FakeHass()
    entry = make_entry()
    await wl._async_update_listener(hass, entry)
    assert hass.config_entries.reloaded == ["e1"]