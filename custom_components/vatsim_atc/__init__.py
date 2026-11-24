"""VATSIM ATC Tracker: follows one VATSIM pilot and reports the ATC around and ahead of them."""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, USER_AGENT, engine_args, signal_for
from .host import Tracker
from .issues import IssueReporter

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.TEXT]

VatsimConfigEntry = ConfigEntry  # runtime_data is the Tracker


def _last_aircraft_file(hass: HomeAssistant, entry_id: str) -> str:
    return hass.config.path(".storage", f"{DOMAIN}.{entry_id}.last_aircraft.json")


async def async_setup_entry(hass: HomeAssistant, entry: VatsimConfigEntry) -> bool:
    session = async_get_clientsession(hass)

    async def fetch(url: str, timeout: float, headers: Optional[dict]) -> str:
        merged = {"User-Agent": USER_AGENT, **(headers or {})}
        async with session.get(url, headers=merged, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            response.raise_for_status()
            return await response.text()

    @callback
    def on_publish(source_id: str) -> None:
        async_dispatcher_send(hass, signal_for(entry.entry_id, source_id))

    async def call_service(service: str, data: dict[str, Any]) -> None:
        domain, _, name = service.partition("/")
        await hass.services.async_call(domain, name, data, blocking=False)

    tracker = Tracker(
        engine_args(dict(entry.data), dict(entry.options)), hass.loop, fetch, on_publish, call_service, _LOGGER,
        last_aircraft_file=_last_aircraft_file(hass, entry.entry_id),
    )
    try:
        await hass.async_add_executor_job(tracker.check_config)
    except ValueError as err:
        await hass.async_add_executor_job(tracker.shutdown)
        raise ConfigEntryError(str(err)) from err

    entry.runtime_data = tracker
    # Platforms first: the ATIS airport text entity restores its value before the engine reads it.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    tracker.start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Repairs issues for things the user should know about (feed unreachable, boundaries or
    # navigation data not loaded, ...). Stopped and cleared when the entry unloads.
    reporter = IssueReporter(hass, entry, tracker)
    reporter.start()
    # Kept where async_remove_entry can find it: Home Assistant removes an entry even when
    # its unload failed, and then the unload callbacks below never run.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = reporter

    @callback
    def stop_reporter() -> None:
        reporter.stop()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    entry.async_on_unload(stop_reporter)

    async def on_stop(event: Event) -> None:
        # Entries are not unloaded when Home Assistant stops. Without this the worker could
        # hold shutdown for one download timeout and the last position would not be saved.
        reporter.pause()
        await hass.async_add_executor_job(tracker.shutdown)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, on_stop))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: VatsimConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VatsimConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Blocking: it waits for a poll in progress, at most one download timeout.
        await hass.async_add_executor_job(entry.runtime_data.shutdown)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: VatsimConfigEntry) -> None:
    # Normally the unload already did both of these. After a failed unload it did neither,
    # and the reporter and the tracker would run on for an entry that no longer exists.
    reporter = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if reporter is not None:
        reporter.stop()
    tracker = getattr(entry, "runtime_data", None)
    if tracker is not None:
        await hass.async_add_executor_job(tracker.shutdown)   # returns at once if already stopped
    path = _last_aircraft_file(hass, entry.entry_id)

    def remove() -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    await hass.async_add_executor_job(remove)
