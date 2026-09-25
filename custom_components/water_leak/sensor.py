"""Sensors for the Water Leak Detector integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LIMIT_MIN, CONF_QUIET_MIN, DOMAIN
from .entity import WaterLeakEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Any,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            WaterLeakActivitySensor(hub, entry),
            WaterLeakLastPulseSensor(hub, entry),
        ]
    )


class WaterLeakActivitySensor(WaterLeakEntity, SensorEntity):
    """Minutes of continuous water activity accumulated."""

    _attr_name = "Continuous activity"
    _attr_native_unit_of_measurement = "min"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:clock-alert"

    def __init__(self, hub, entry) -> None:
        super().__init__(hub, entry)
        self._attr_unique_id = f"{entry.entry_id}-activity"

    def update_from_hub(self) -> None:
        self._attr_native_value = round(self.hub.activity, 1)
        self._attr_extra_state_attributes = {
            CONF_QUIET_MIN: self.hub.quiet_min,
            CONF_LIMIT_MIN: self.hub.limit_min,
            "water_meter": self.hub.water_meter,
            "last_meter_value": self.hub.last_value,
        }


class WaterLeakLastPulseSensor(WaterLeakEntity, SensorEntity):
    """Timestamp of the last time the meter incremented."""

    _attr_name = "Last pulse"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:water-pump"

    def __init__(self, hub, entry) -> None:
        super().__init__(hub, entry)
        self._attr_unique_id = f"{entry.entry_id}-last-pulse"

    def update_from_hub(self) -> None:
        self._attr_native_value = self.hub.last_pulse_iso