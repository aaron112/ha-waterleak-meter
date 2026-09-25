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


async def test_async_unload_entry_tears_down_hub():
    hass = FakeHass()
    entry = make_entry()
    await wl.async_setup_entry(hass, entry)
    hub = hass.data[DOMAIN]["e1"]
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