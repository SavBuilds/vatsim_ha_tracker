"""Sends the flight alerts and the pushes for problems: the part the alerts package used to do.

core/alerts.py decides when an alert is due and what it says. This file listens to what the
engine publishes, applies the user's choices (alert mode, the switch per alert, the switch per
speaker, the entity that holds speech) and makes the service calls.

A call that fails is logged and remembered, never raised: one dead speaker must not stop the
rest. What is remembered is reported as a repair by issues.py, so a failure is not silent.
"""
from __future__ import annotations

import logging
from functools import partial
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .const import (
    CONF_MUTE_GATE, CONF_NOTIFY_TARGETS, CONF_PUSH_REPAIRS, CONF_SPEAKERS, CONF_TTS_ENTITY,
    DEFAULT_NOTIFY_TARGETS, ENTITY_TARGET_PREFIX, SERVICE_TARGET_PREFIX, SIDEBAR_TARGET, signal_for,
)
from .core import alerts as rules
from .core.problems import PUSH_TITLES
from .host import Tracker

_LOGGER = logging.getLogger(__name__)

TEST = "test"
SPEECH_HELD_NOTICE = "vatsim_speech_held"


class AlertManager:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tracker: Tracker) -> None:
        self._hass, self._entry, self._tracker = hass, entry, tracker
        options = entry.options
        self.notify_targets: List[str] = list(options.get(CONF_NOTIFY_TARGETS, DEFAULT_NOTIFY_TARGETS))
        self.speaker_players: List[str] = list(options.get(CONF_SPEAKERS, ()))
        self.tts_entity: Optional[str] = options.get(CONF_TTS_ENTITY) or None
        self.gate_entity: Optional[str] = options.get(CONF_MUTE_GATE) or None
        self.push_repairs: bool = bool(options.get(CONF_PUSH_REPAIRS, True))
        # Set by the select and switch entities, which restore their last value at start.
        self.mode: str = rules.DEFAULT_MODE
        self.enabled: Dict[str, bool] = {kind: True for kind in rules.KINDS}
        self.speakers: Dict[str, bool] = {player: True for player in self.speaker_players}
        # Text of the last push or speech call that failed, until a later one works.
        self.push_error: Optional[str] = None
        self.speech_error: Optional[str] = None
        self._previous: Dict[str, Any] = {}
        self._unsubscribe: List[Callable[[], None]] = []
        self._cancel_confirm: Optional[Callable[[], None]] = None

    # ---- lifecycle -------------------------------------------------------------------------

    @callback
    def start(self) -> None:
        for source in rules.SOURCES:
            self._previous[source] = self._tracker.published.get(source)
            # A partial of a @callback method is still run on the event loop. A lambda is not
            # recognised as one, and Home Assistant would run it in a worker thread.
            self._unsubscribe.append(async_dispatcher_connect(
                self._hass, signal_for(self._entry.entry_id, source), partial(self._on_publish, source)))

    @callback
    def stop(self) -> None:
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe.clear()
        if self._cancel_confirm is not None:
            self._cancel_confirm()
            self._cancel_confirm = None

    # ---- what the engine published -----------------------------------------------------------

    @callback
    def _on_publish(self, source: str) -> None:
        published = self._tracker.published
        old, new = self._previous.get(source), published.get(source)
        if new is old:
            return                      # two publishes close together: the first call saw both
        self._previous[source] = new
        if source == rules.COVERAGE:
            if rules.controller_online_started(old, new):
                # Starting again replaces a wait already running, as the automation did.
                if self._cancel_confirm is not None:
                    self._cancel_confirm()
                self._cancel_confirm = async_call_later(
                    self._hass, rules.CONTROLLER_ONLINE_CONFIRM_SECONDS, self._confirm_controller_online)
        elif source == rules.NEXT_SECTOR:
            self._send(rules.NEXT_CONTROLLER, rules.next_controller_message(old, new))
        elif source == rules.DESTINATION:
            pilot = published.get(rules.PILOT_CONNECTED, (None, {}))[0]
            self._send(rules.ATIS_CHANGE, rules.atis_change_message(old, new, pilot == "on"))

    @callback
    def _confirm_controller_online(self, _now: Any = None) -> None:
        self._cancel_confirm = None
        self._send(rules.CONTROLLER_ONLINE,
                   rules.controller_online_message(self._tracker.published.get(rules.COVERAGE)))

    @callback
    def _send(self, kind: str, message: Optional[Tuple[str, str]]) -> None:
        if message is not None and self.enabled.get(kind, True):
            self._hass.async_create_task(self.async_announce(message[0], message[1], self.mode))

    # ---- sending -----------------------------------------------------------------------------

    def gate(self) -> Optional[Tuple[str, str]]:
        """(entity id, state) of the entity that holds speech, or None when none is set."""
        if not self.gate_entity:
            return None
        state = self._hass.states.get(self.gate_entity)
        return self.gate_entity, ("missing" if state is None else str(state.state))

    def problem_inputs(self) -> Mapping[str, Any]:
        """What core/problems.py needs from here."""
        errors = [e for e in (self.push_error, self.speech_error) if e]
        return {"gate": self.gate(), "delivery_error": "; ".join(errors) or None}

    async def async_test(self) -> None:
        """The test button: a push and speech whatever the alert mode is, so the whole path
        can be checked without a flight. The speaker switches and the hold entity still apply."""
        await self.async_announce("VATSIM test alert", "This is a test of the VATSIM alerts.",
                                  rules.MODE_PUSH_AND_SPEAKER)

    async def async_announce(self, title: str, message: str, mode: str) -> None:
        speech = rules.speech_enabled(mode)
        players = [p for p in self.speaker_players if self.speakers.get(p, True)] if self.tts_entity else []
        gate = self.gate()
        gate_state = gate[1] if gate else None
        held = speech and bool(players) and rules.mute_gate_holds(gate_state)
        if rules.push_enabled(mode):
            note = f" (speakers held: {gate[0]} is {gate_state})" if held and gate else ""
            await self.async_push(title, message + note, tag=None, clears=True)
        if held and gate and rules.mute_gate_broken(gate_state):
            # A broken hold entity is not what the hold exists for. Say so where it will be seen;
            # the repair follows if it stays broken.
            await self._call("persistent_notification", "create", {
                "notification_id": SPEECH_HELD_NOTICE, "title": "VATSIM speech held",
                "message": f"{gate[0]} is {gate_state}, so VATSIM speech was skipped: {message}"})
        if not speech or not players:
            return
        failed: List[str] = []
        spoken = 0
        for player in players:
            # Checked again before every speaker: a hold that starts part way stops the rest.
            now = self.gate()
            if rules.mute_gate_holds(now[1] if now else None):
                break
            error = await self._call("tts", "speak", {
                "entity_id": self.tts_entity, "media_player_entity_id": player, "message": message})
            spoken += 1
            if error:
                failed.append(f"speech on {player}: {error}")
        if failed:
            self.speech_error = "; ".join(failed)[:300]
        elif spoken:
            self.speech_error = None

    async def async_push(self, title: str, message: str, tag: Optional[str], clears: bool,
                         skip_sidebar: bool = False) -> None:
        failed: List[str] = []
        sent = 0
        for target in self.notify_targets:
            if skip_sidebar and target == SIDEBAR_TARGET:
                continue
            if target.startswith(SERVICE_TARGET_PREFIX):
                service = target[len(SERVICE_TARGET_PREFIX):]
                data: Dict[str, Any] = {"title": title, "message": message}
                # Notices with one tag replace each other. Only these two kinds of target are
                # known to take one; any other notify action gets the plain message.
                if tag and service == "persistent_notification":
                    data["data"] = {"notification_id": tag}
                elif tag and service.startswith("mobile_app_"):
                    data["data"] = {"tag": tag}
                error = await self._call("notify", service, data)
            elif target.startswith(ENTITY_TARGET_PREFIX):
                error = await self._call("notify", "send_message", {
                    "entity_id": target[len(ENTITY_TARGET_PREFIX):], "title": title, "message": message})
            else:
                error = "not a notify action or entity"
            sent += 1
            if error:
                failed.append(f"push to {target.partition(':')[2] or target}: {error}")
        if failed:
            self.push_error = "; ".join(failed)[:300]
        elif sent and clears:
            self.push_error = None

    @callback
    def problem_raised(self, key: str) -> None:
        """Called by the issue reporter when a repair is first raised. Sent whatever the alert
        mode is: switching alerts off must never hide a fault. The sidebar notice is skipped,
        because the repair is already on screen there."""
        if not self.push_repairs:
            return
        title = PUSH_TITLES.get(key, "VATSIM tracker problem")
        self._hass.async_create_task(self.async_push(
            title, "Open Settings > System > Repairs for what to check.", tag=f"vatsim_{key}",
            clears=False, skip_sidebar=True))

    async def _call(self, domain: str, service: str, data: Dict[str, Any]) -> Optional[str]:
        """Make one service call. Returns the error text, or None when it worked."""
        try:
            await self._hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # whatever the other integration raised
            _LOGGER.warning("VATSIM alert: %s.%s failed: %s", domain, service, err)
            return f"{type(err).__name__}: {err}"[:200]
        return None
