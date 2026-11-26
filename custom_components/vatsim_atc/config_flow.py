"""Setup asks for one thing, the VATSIM CID. Everything else is an option."""
from __future__ import annotations

import os
import re
from typing import Any, Optional

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from homeassistant.helpers import selector

from .const import (
    ALERT_OPTION_KEYS, CONF_CID, CONF_LOOKAHEAD_MINUTES, CONF_METAR_FALLBACK, CONF_MUTE_GATE,
    CONF_NAVDATA_PATH, CONF_NOTIFY_TARGETS, CONF_PUSH_REPAIRS, CONF_SIMAWARE_PATH, CONF_SPEAKERS,
    CONF_TTS_ENTITY, DEFAULT_NOTIFY_TARGETS, DOMAIN, ENTITY_TARGET_PREFIX, OPTION_KEYS, SERVICE_TARGET_PREFIX,
)
from .core.engine import DEFAULT_LOOKAHEAD_MINUTES

CID_RE = re.compile(r"\d{5,8}")
PATH_KEYS = (CONF_NAVDATA_PATH, CONF_SIMAWARE_PATH)


class VatsimAtcConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: Optional[dict[str, Any]] = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cid = str(user_input[CONF_CID]).strip()
            if not CID_RE.fullmatch(cid):
                errors[CONF_CID] = "invalid_cid"
            else:
                await self.async_set_unique_id(cid)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"VATSIM {cid}", data={CONF_CID: cid})
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required(CONF_CID): str}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return VatsimAtcOptionsFlow()


# Notify actions that are not a place to send to: the entity action, and the catch-all that
# goes to whichever notifier was set up first.
NOT_TARGETS = frozenset({"send_message", "notify"})


def notify_choices(hass: Any, chosen: list) -> list:
    """Everything a push can go to on this system: the older notify actions (which can replace
    an earlier notice) and notify entities. Anything already chosen stays in the list even if
    it has gone, so saving the form does not silently drop it."""
    choices = {}
    for service in sorted(hass.services.async_services_for_domain("notify")):
        if service not in NOT_TARGETS:
            label = "Sidebar notice" if service == "persistent_notification" else f"notify.{service}"
            choices[SERVICE_TARGET_PREFIX + service] = label
    for state in sorted(hass.states.async_all("notify"), key=lambda s: s.entity_id):
        choices[ENTITY_TARGET_PREFIX + state.entity_id] = f"{state.name} ({state.entity_id})"
    for value in chosen:
        choices.setdefault(value, f"{value.partition(':')[2] or value} (not found)")
    return [selector.SelectOptionDict(value=value, label=label) for value, label in choices.items()]


class VatsimAtcOptionsFlow(OptionsFlow):
    """Two short forms behind a menu. Each saves only its own keys and keeps the other's."""

    async def async_step_init(self, user_input: Optional[dict[str, Any]] = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["sources", "alerts"])

    def _save(self, keys: tuple, cleaned: dict[str, Any]) -> ConfigFlowResult:
        kept = {k: v for k, v in self.config_entry.options.items() if k not in keys}
        return self.async_create_entry(data={**kept, **cleaned})

    async def async_step_sources(self, user_input: Optional[dict[str, Any]] = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = dict(user_input)
            for key in PATH_KEYS:
                value = str(cleaned.get(key) or "").strip()
                if not value:
                    cleaned.pop(key, None)     # blank means "not used"
                    continue
                cleaned[key] = value
                if not await self.hass.async_add_executor_job(os.path.exists, value):
                    errors[key] = "path_not_found"
            if not errors:
                return self._save(OPTION_KEYS, cleaned)
        current = user_input if user_input is not None else dict(self.config_entry.options)
        schema = vol.Schema({
            vol.Optional(CONF_NAVDATA_PATH, description={"suggested_value": current.get(CONF_NAVDATA_PATH, "")}): str,
            vol.Optional(CONF_SIMAWARE_PATH, description={"suggested_value": current.get(CONF_SIMAWARE_PATH, "")}): str,
            vol.Optional(CONF_LOOKAHEAD_MINUTES,
                         default=current.get(CONF_LOOKAHEAD_MINUTES, DEFAULT_LOOKAHEAD_MINUTES)):
                vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
            vol.Optional(CONF_METAR_FALLBACK, default=current.get(CONF_METAR_FALLBACK, True)): bool,
        })
        return self.async_show_form(step_id="sources", data_schema=schema, errors=errors)

    async def async_step_alerts(self, user_input: Optional[dict[str, Any]] = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = {
                # Saved even when empty: an empty list means "no pushes", a missing key means
                # "never chosen", which sends to the sidebar.
                CONF_NOTIFY_TARGETS: list(user_input.get(CONF_NOTIFY_TARGETS) or []),
                CONF_SPEAKERS: list(user_input.get(CONF_SPEAKERS) or []),
                CONF_PUSH_REPAIRS: bool(user_input.get(CONF_PUSH_REPAIRS, True)),
            }
            for key in (CONF_TTS_ENTITY, CONF_MUTE_GATE):
                if user_input.get(key):
                    cleaned[key] = user_input[key]
            if cleaned[CONF_SPEAKERS] and CONF_TTS_ENTITY not in cleaned:
                errors[CONF_TTS_ENTITY] = "tts_needed"
            if not errors:
                return self._save(ALERT_OPTION_KEYS, cleaned)
        current = user_input if user_input is not None else dict(self.config_entry.options)
        chosen = list(current.get(CONF_NOTIFY_TARGETS, DEFAULT_NOTIFY_TARGETS))
        entity = lambda **config: selector.EntitySelector(selector.EntitySelectorConfig(**config))  # noqa: E731
        schema = vol.Schema({
            vol.Optional(CONF_NOTIFY_TARGETS, default=chosen): selector.SelectSelector(selector.SelectSelectorConfig(
                options=notify_choices(self.hass, chosen), multiple=True,
                mode=selector.SelectSelectorMode.DROPDOWN)),
            vol.Optional(CONF_SPEAKERS, default=list(current.get(CONF_SPEAKERS, []))):
                entity(filter={"domain": "media_player"}, multiple=True),
            vol.Optional(CONF_TTS_ENTITY, description={"suggested_value": current.get(CONF_TTS_ENTITY)}):
                entity(filter={"domain": "tts"}),
            vol.Optional(CONF_MUTE_GATE, description={"suggested_value": current.get(CONF_MUTE_GATE)}):
                entity(filter={"domain": ["binary_sensor", "input_boolean", "switch"]}),
            vol.Optional(CONF_PUSH_REPAIRS, default=bool(current.get(CONF_PUSH_REPAIRS, True))):
                selector.BooleanSelector(),
        })
        return self.async_show_form(step_id="alerts", data_schema=schema, errors=errors)
