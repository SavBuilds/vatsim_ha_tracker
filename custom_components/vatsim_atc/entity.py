"""Base class: one Home Assistant entity showing one state the engine publishes."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DEVICE_NAME, DOMAIN, HIDDEN_ATTRIBUTES, UNRECORDED_ATTRIBUTES, VERSION, signal_for
from .host import Tracker


def device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=DEVICE_NAME,
        manufacturer="VATSIM",
        model="ATC tracker",
        sw_version=VERSION,
        entry_type=DeviceEntryType.SERVICE,
    )


class VatsimEntity(Entity):
    """source_id is the engine's own id for the state, for example sensor.vatsim_feed_health.
    It is a lookup key only; the entity id Home Assistant assigns comes from the device and name."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _unrecorded_attributes = UNRECORDED_ATTRIBUTES
    _hidden_attributes = HIDDEN_ATTRIBUTES

    def __init__(self, entry: ConfigEntry, tracker: Tracker, source_id: str, key: str) -> None:
        self._tracker = tracker
        self._source_id = source_id
        self._signal = signal_for(entry.entry_id, source_id)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = device_info(entry)

    @property
    def _published(self) -> tuple[Any, dict[str, Any]]:
        return self._tracker.published.get(self._source_id, (None, {}))

    @property
    def available(self) -> bool:
        # Unavailable until the engine has published this state at least once.
        return self._source_id in self._tracker.published

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {k: v for k, v in self._published[1].items() if k not in self._hidden_attributes}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal, self._handle_publish))

    @callback
    def _handle_publish(self) -> None:
        self.async_write_ha_state()
