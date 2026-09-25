"""Binary sensor for the Water Leak Detection for Meters integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import WaterLeakEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Any,
) -> None:
    hub = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WaterLeakDetectedSensor(hub, entry)])


class WaterLeakDetectedSensor(WaterLeakEntity, BinarySensorEntity):
    """True while a leak is active (continuous activity past the limit)."""

    _attr_name = "Leak detected"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, hub, entry) -> None:
        super().__init__(hub, entry)
        self._attr_unique_id = f"{entry.entry_id}-leak"

    def update_from_hub(self) -> None:
        self._attr_is_on = self.hub.leak_active
        self._attr_icon = "mdi:water-alert" if self.hub.leak_active else "mdi:water-check"