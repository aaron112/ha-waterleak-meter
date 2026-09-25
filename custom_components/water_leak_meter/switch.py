"""Switch for the Water Leak Detection for Meters integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    async_add_entities([WaterLeakSuppressSwitch(hub, entry)])


class WaterLeakSuppressSwitch(WaterLeakEntity, SwitchEntity):
    """Suppress leak alerts (e.g. while watering the garden).

    Deliberately NOT persisted: suppression is for a time-boxed activity, and a
    forgotten toggle must not silence a safety device indefinitely. A restart
    re-enables alerts, which is the safe direction to fail.
    """

    _attr_name = "Suppress alerts"
    _attr_icon = "mdi:water-off-outline"

    def __init__(self, hub, entry) -> None:
        super().__init__(hub, entry)
        self._attr_unique_id = f"{entry.entry_id}-suppress"

    def update_from_hub(self) -> None:
        self._attr_is_on = self.hub.suppressed

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.hub.set_suppressed(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.hub.set_suppressed(False)