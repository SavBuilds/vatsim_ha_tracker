"""The alert mode: where flight alerts go (nowhere, push, speakers, or both)."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .alerts import AlertManager
from .core import alerts as rules
from .entity import device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([AlertModeSelect(entry, entry.runtime_data.alerts)])


class AlertModeSelect(SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "alert_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(rules.MODES)

    def __init__(self, entry: ConfigEntry, alerts: AlertManager) -> None:
        self._alerts = alerts
        self._attr_unique_id = f"{entry.entry_id}_alert_mode"
        self._attr_device_info = device_info(entry)
        self._attr_current_option = alerts.mode

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in rules.MODES:
            self._attr_current_option = self._alerts.mode = last.state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = self._alerts.mode = option
        self.async_write_ha_state()
