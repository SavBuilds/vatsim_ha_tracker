"""The test button: sends one alert through the real push and speech path, without a flight."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .alerts import AlertManager
from .entity import device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([TestAlertButton(entry, entry.runtime_data.alerts)])


class TestAlertButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "test_alert"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, alerts: AlertManager) -> None:
        self._alerts = alerts
        self._attr_unique_id = f"{entry.entry_id}_test_alert"
        self._attr_device_info = device_info(entry)

    async def async_press(self) -> None:
        await self._alerts.async_test()
