"""Shared entity base for the Water Leak Detector integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity

from . import WaterLeakHub
from .const import DOMAIN


class WaterLeakEntity(Entity):
    """Entity attached to a WaterLeakHub, refreshed by the hub on changes."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: WaterLeakHub, entry: ConfigEntry) -> None:
        self.hub = hub
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Water Leak Detector",
            manufacturer="Water Leak",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hub.add_entity(self)

    def update_from_hub(self) -> None:
        raise NotImplementedError