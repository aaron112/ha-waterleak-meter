"""Unit tests for entities: base, sensors, binary sensors and the switch."""

import pytest

from conftest import make_entry, make_hub

from custom_components.water_leak_meter import binary_sensor as bin_mod
from custom_components.water_leak_meter import sensor as sens_mod
from custom_components.water_leak_meter import switch as sw_mod
from custom_components.water_leak_meter.binary_sensor import BinarySensorDeviceClass
from custom_components.water_leak_meter.const import (
    CONF_LIMIT_MIN,
    CONF_PULSE_FT3,
    CONF_QUIET_MIN,
    DOMAIN,
)
from custom_components.water_leak_meter.entity import WaterLeakEntity
from custom_components.water_leak_meter.sensor import SensorDeviceClass, SensorStateClass


class _Concrete(WaterLeakEntity):
    def update_from_hub(self) -> None:
        self.updated = True


# --- shared base ------------------------------------------------------------

async def test_base_entity_registers_and_writes_to_hub():
    hub = make_hub()
    e = _Concrete(hub, make_entry())
    assert e.hub is hub
    assert e._attr_has_entity_name is True
    assert e._attr_should_poll is False
    info = e._attr_device_info.info
    assert info["identifiers"] == {(DOMAIN, "e1")}
    assert info["name"] == "Water Leak Detector"
    assert info["manufacturer"] == "Water Leak"
    with pytest.raises(NotImplementedError):
        WaterLeakEntity(hub, make_entry()).update_from_hub()
    assert hub._entities == []
    await e.async_added_to_hass()
    assert hub._entities == [e]
    assert e.updated is True
    assert e._writes == 1


# --- sensors ----------------------------------------------------------------

async def test_activity_sensor_attributes_and_update():
    hub = make_hub()
    s = sens_mod.WaterLeakActivitySensor(hub, make_entry())
    assert s._attr_unique_id == "e1-activity"
    assert s._attr_name == "Continuous activity"
    assert s._attr_native_unit_of_measurement == "min"
    assert s._attr_device_class == SensorDeviceClass.DURATION
    assert s._attr_state_class == SensorStateClass.MEASUREMENT
    assert s._attr_icon == "mdi:clock-alert"
    hub.activity = 12.5
    hub.last_value = 100.0
    s.update_from_hub()
    assert s._attr_native_value == 12.5
    attrs = s._attr_extra_state_attributes
    assert attrs[CONF_QUIET_MIN] == 45
    assert attrs[CONF_LIMIT_MIN] == 120
    assert attrs[CONF_PULSE_FT3] == 2.0
    assert attrs["min_detectable_leak_l_day"] == 1812.3
    assert attrs["water_meter"] == "sensor.meter"
    assert attrs["last_meter_value"] == 100.0


async def test_last_pulse_sensor_uses_timezone_aware_datetime():
    hub = make_hub()
    s = sens_mod.WaterLeakLastPulseSensor(hub, make_entry())
    assert s._attr_unique_id == "e1-last-pulse"
    assert s._attr_name == "Last pulse"
    assert s._attr_device_class == SensorDeviceClass.TIMESTAMP
    s.update_from_hub()
    assert s._attr_native_value is None  # unknown until the first real pulse
    hub.last_pulse_ts = 1700000000.0
    s.update_from_hub()
    assert s._attr_native_value is not None
    assert getattr(s._attr_native_value, "tzinfo", None) is not None


# --- binary sensors ---------------------------------------------------------

async def test_leak_detected_sensor_states():
    hub = make_hub()
    s = bin_mod.WaterLeakDetectedSensor(hub, make_entry())
    assert s._attr_unique_id == "e1-leak"
    assert s._attr_name == "Leak detected"
    assert s._attr_device_class == BinarySensorDeviceClass.PROBLEM
    hub.leak_active = False
    s.update_from_hub()
    assert s._attr_is_on is False
    assert s._attr_icon == "mdi:water-check"
    hub.leak_active = True
    s.update_from_hub()
    assert s._attr_is_on is True
    assert s._attr_icon == "mdi:water-alert"


async def test_meter_signal_sensor_states():
    hub = make_hub()
    s = bin_mod.WaterLeakSignalSensor(hub, make_entry())
    assert s._attr_unique_id == "e1-signal"
    assert s._attr_name == "Meter signal"
    hub.signal_lost = True
    s.update_from_hub()
    assert s._attr_is_on is True
    assert s._attr_icon == "mdi:wifi-off"
    hub.signal_lost = False
    s.update_from_hub()
    assert s._attr_is_on is False
    assert s._attr_icon == "mdi:wifi-arrow-up"


# --- switch ----------------------------------------------------------------

async def test_suppress_switch():
    hub = make_hub()
    s = sw_mod.WaterLeakSuppressSwitch(hub, make_entry())
    assert s._attr_unique_id == "e1-suppress"
    assert s._attr_name == "Suppress alerts"
    assert s._attr_icon == "mdi:water-off-outline"
    hub.suppressed = False
    s.update_from_hub()
    assert s._attr_is_on is False
    await s.async_turn_on(arbitrary="kw")
    assert hub.suppressed is True
    s.update_from_hub()
    assert s._attr_is_on is True
    await s.async_turn_off()
    assert hub.suppressed is False
    s.update_from_hub()
    assert s._attr_is_on is False


async def test_entity_removal_unregisters_from_hub():
    """A removed entity must not stay in the hub's fan-out list forever."""
    hub = make_hub()
    s = _Concrete(hub, make_entry())
    await s.async_added_to_hass()
    assert hub._entities == [s]
    await s.async_will_remove_from_hass()
    assert hub._entities == []
    # removing twice must not raise
    await s.async_will_remove_from_hass()
    assert hub._entities == []


# --- platform setup ---------------------------------------------------------

async def test_sensor_platform_setup_entry():
    hub = make_hub()
    hub.hass.data[DOMAIN] = {"e1": hub}
    added: list = []
    await sens_mod.async_setup_entry(hub.hass, make_entry(), added.extend)
    assert len(added) == 2
    assert isinstance(added[0], sens_mod.WaterLeakActivitySensor)
    assert isinstance(added[1], sens_mod.WaterLeakLastPulseSensor)


async def test_binary_sensor_platform_setup_entry():
    hub = make_hub()
    hub.hass.data[DOMAIN] = {"e1": hub}
    added: list = []
    await bin_mod.async_setup_entry(hub.hass, make_entry(), added.extend)
    assert len(added) == 2
    assert isinstance(added[0], bin_mod.WaterLeakDetectedSensor)
    assert isinstance(added[1], bin_mod.WaterLeakSignalSensor)


async def test_switch_platform_setup_entry():
    hub = make_hub()
    hub.hass.data[DOMAIN] = {"e1": hub}
    added: list = []
    await sw_mod.async_setup_entry(hub.hass, make_entry(), added.extend)
    assert len(added) == 1
    assert isinstance(added[0], sw_mod.WaterLeakSuppressSwitch)


# --- hub fan-out to registered entities -------------------------------------

async def test_pulse_updates_registered_entities(wl, clock):
    hub = make_hub()
    await hub.async_load()
    s = sens_mod.WaterLeakActivitySensor(hub, make_entry())
    await s.async_added_to_hass()
    clock.now = 0.0
    await hub._on_meter_change(wl_event(1.0))
    assert s._attr_native_value is not None
    assert s._writes >= 1


async def test_leak_fire_pushes_entities_immediately(wl, clock):
    hub = make_hub()
    await hub.async_load()
    s = bin_mod.WaterLeakDetectedSensor(hub, make_entry())
    await s.async_added_to_hass()
    # stop exactly on the pulse that crosses the limit (40+40+40 = 120 min):
    # no later event may arrive to publish the flip, so only the firing path
    # can make the new state visible
    for n, minute in enumerate((40, 80, 120, 160), start=1):
        clock.now = minute * 60.0
        await hub._on_meter_change(wl_event(n))
    assert hub.leak_active is True
    assert s._attr_is_on is True
    # the crossing pulse wrote twice (its normal push, then the post-flip one):
    # dropping the post-flip call would leave the sensor OFF for the episode
    assert s._writes == 2 + len((40, 80, 120, 160))


def wl_event(value: float):
    from types import SimpleNamespace as NS

    return NS(data={"new_state": NS(state=str(value))})