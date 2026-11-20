"""Setup asks for one thing, the VATSIM CID. Everything else is an option."""
from __future__ import annotations

import os
import re
from typing import Any, Optional

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import (
    CONF_CID, CONF_LOOKAHEAD_MINUTES, CONF_METAR_FALLBACK, CONF_NAVDATA_PATH, CONF_SIMAWARE_PATH, DOMAIN,
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


class VatsimAtcOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: Optional[dict[str, Any]] = None) -> ConfigFlowResult:
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
                return self.async_create_entry(data=cleaned)
        current = user_input if user_input is not None else dict(self.config_entry.options)
        schema = vol.Schema({
            vol.Optional(CONF_NAVDATA_PATH, description={"suggested_value": current.get(CONF_NAVDATA_PATH, "")}): str,
            vol.Optional(CONF_SIMAWARE_PATH, description={"suggested_value": current.get(CONF_SIMAWARE_PATH, "")}): str,
            vol.Optional(CONF_LOOKAHEAD_MINUTES,
                         default=current.get(CONF_LOOKAHEAD_MINUTES, DEFAULT_LOOKAHEAD_MINUTES)):
                vol.All(vol.Coerce(float), vol.Range(min=1, max=60)),
            vol.Optional(CONF_METAR_FALLBACK, default=current.get(CONF_METAR_FALLBACK, True)): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
