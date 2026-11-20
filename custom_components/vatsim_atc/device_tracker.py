"""The aircraft as a device tracker, so any map card can follow it."""
from __future__ import annotations

from typing import Optional

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import HIDDEN_ATTRIBUTES, TRACKER_PROPERTY_ATTRIBUTES
from .core.engine import AIRCRAFT_TRACKER_ENTITY_ID
from .entity import VatsimEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([VatsimAircraftTracker(entry, entry.runtime_data, AIRCRAFT_TRACKER_ENTITY_ID, "aircraft")])


class VatsimAircraftTracker(VatsimEntity, TrackerEntity):
    # Position is reported through the properties below, not repeated as extra attributes.
    _hidden_attributes = HIDDEN_ATTRIBUTES | TRACKER_PROPERTY_ATTRIBUTES

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> Optional[float]:
        return self._coordinate("latitude")

    @property
    def longitude(self) -> Optional[float]:
        return self._coordinate("longitude")

    @property
    def location_accuracy(self) -> int:
        return 0

    def _coordinate(self, key: str) -> Optional[float]:
        value = self._published[1].get(key)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None
