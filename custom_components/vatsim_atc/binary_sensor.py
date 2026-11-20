"""Binary sensors: pilot connected, and ATC coverage at the aircraft's position."""
from __future__ import annotations

from typing import Any, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import VatsimEntity

# (engine id, key, device class)
BINARY_SENSORS = (
    ("binary_sensor.vatsim_pilot_connected", "pilot_connected", BinarySensorDeviceClass.CONNECTIVITY),
    ("binary_sensor.vatsim_atc_coverage", "atc_coverage", None),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities(VatsimBinarySensor(entry, entry.runtime_data, source_id, key, device_class)
                       for source_id, key, device_class in BINARY_SENSORS)


class VatsimBinarySensor(VatsimEntity, BinarySensorEntity):
    def __init__(self, entry: ConfigEntry, tracker: Any, source_id: str, key: str,
                 device_class: Optional[BinarySensorDeviceClass]) -> None:
        super().__init__(entry, tracker, source_id, key)
        self._attr_device_class = device_class

    @property
    def is_on(self) -> Optional[bool]:
        state = self._published[0]
        return None if state is None else state == "on"
