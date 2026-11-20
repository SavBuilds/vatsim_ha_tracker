"""The ATIS airport box: type an ICAO code to pin the ATIS card to that airport, blank for automatic."""
from __future__ import annotations

import re

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .core.engine import ATIS_AIRPORT_OVERRIDE_ENTITY_ID
from .entity import device_info
from .host import Tracker


PATTERN = r"^[A-Za-z0-9]{0,4}$"
VALUE_RE = re.compile(PATTERN)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([AtisAirportText(entry, entry.runtime_data)])


class AtisAirportText(TextEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "atis_airport"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = TextMode.TEXT
    _attr_native_min = 0
    _attr_native_max = 4
    _attr_pattern = PATTERN

    def __init__(self, entry: ConfigEntry, tracker: Tracker) -> None:
        self._tracker = tracker
        self._attr_unique_id = f"{entry.entry_id}_atis_airport"
        self._attr_device_info = device_info(entry)
        self._attr_native_value = ""

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            restored = last.state.strip().upper()
            # A restored value that breaks the pattern would make every state write fail.
            self._attr_native_value = restored if VALUE_RE.fullmatch(restored) else ""
        # The engine reads the box under its old helper's id.
        self._tracker.set_local_state(ATIS_AIRPORT_OVERRIDE_ENTITY_ID, self._attr_native_value)

    async def async_set_value(self, value: str) -> None:
        self._attr_native_value = value.strip().upper()
        self._tracker.set_local_state(ATIS_AIRPORT_OVERRIDE_ENTITY_ID, self._attr_native_value)
        self.async_write_ha_state()
