"""Sensors: feed health, next sector, destination, and the three map layers."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .core.engine import (
    DESTINATION_ENTITY_ID, FLIGHT_ROUTE_ENTITY_ID, ONLINE_ATC_BOUNDARIES_ENTITY_ID, SESSION_TRACK_ENTITY_ID,
)
from .entity import VatsimEntity

# (engine id, key, diagnostic)
SENSORS = (
    ("sensor.vatsim_feed_health", "feed_health", True),
    ("sensor.vatsim_next_sector", "next_sector", False),
    (DESTINATION_ENTITY_ID, "destination", False),
    (ONLINE_ATC_BOUNDARIES_ENTITY_ID, "online_atc_boundaries", False),
    (FLIGHT_ROUTE_ENTITY_ID, "flight_route", False),
    (SESSION_TRACK_ENTITY_ID, "session_track", False),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(VatsimSensor(entry, entry.runtime_data, source_id, key, diagnostic)
                       for source_id, key, diagnostic in SENSORS)


class VatsimSensor(VatsimEntity, SensorEntity):
    def __init__(self, entry: ConfigEntry, tracker: Any, source_id: str, key: str, diagnostic: bool) -> None:
        super().__init__(entry, tracker, source_id, key)
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> Any:
        state = self._published[0]
        return None if state is None else str(state)
