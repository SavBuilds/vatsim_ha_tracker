"""Switches: one per flight alert, and one per speaker chosen in the integration's options."""
from __future__ import annotations

from typing import Any, Dict

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .alerts import AlertManager
from .core import alerts as rules
from .entity import device_info

SPEAKER_ID = "speaker_"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    alerts: AlertManager = entry.runtime_data.alerts
    # A speaker taken out of the options leaves its switch behind in the registry. Remove those.
    wanted = {f"{entry.entry_id}_{SPEAKER_ID}{player}" for player in alerts.speaker_players}
    registry = er.async_get(hass)
    for known in er.async_entries_for_config_entry(registry, entry.entry_id):
        if known.domain == "switch" and known.unique_id.startswith(f"{entry.entry_id}_{SPEAKER_ID}") \
                and known.unique_id not in wanted:
            registry.async_remove(known.entity_id)
    entities = [VatsimSwitch(entry, alerts.enabled, kind, f"alert_{kind}", None) for kind in rules.KINDS]
    for player in alerts.speaker_players:
        state = hass.states.get(player)
        name = (state.name if state is not None else None) or player.partition(".")[2].replace("_", " ")
        entities.append(VatsimSwitch(entry, alerts.speakers, player, SPEAKER_ID + player, name))
    async_add_entities(entities)


class VatsimSwitch(SwitchEntity, RestoreEntity):
    """On or off for one key of a dict the alert sender reads. On until the user says otherwise."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, store: Dict[str, bool], key: str, unique: str, speaker_name: Any) -> None:
        self._store, self._key = store, key
        self._attr_unique_id = f"{entry.entry_id}_{unique}"
        self._attr_device_info = device_info(entry)
        if speaker_name is None:
            self._attr_translation_key = unique
        else:
            self._attr_translation_key = "speaker"
            self._attr_translation_placeholders = {"name": str(speaker_name)}

    @property
    def is_on(self) -> bool:
        return self._store.get(self._key, True)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in ("on", "off"):
            self._store[self._key] = last.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._store[self._key] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._store[self._key] = False
        self.async_write_ha_state()
