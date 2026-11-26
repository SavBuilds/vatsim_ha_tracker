"""Turns the tracker's problems into Home Assistant Repairs issues, and clears them again.

The rules are in core/problems.py. This file only runs them on a timer and talks to the issue
registry. Issues are not persistent: after a restart the integration raises again whatever
still applies.
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, PROJECT_URL
from .core.problems import ERRORS, ProblemWatch
from .host import Tracker

CHECK_INTERVAL = timedelta(seconds=30)
FEED_HEALTH = "sensor.vatsim_feed_health"
PILOT_CONNECTED = "binary_sensor.vatsim_pilot_connected"


class IssueReporter:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tracker: Tracker,
                 alert_inputs: Optional[Callable[[], Mapping[str, Any]]] = None,
                 on_raised: Optional[Callable[[str], None]] = None) -> None:
        """alert_inputs supplies what the alert sender knows (the hold entity's state, a failed
        delivery). on_raised is called once when a problem is first reported, not when its text
        is updated."""
        self._hass, self._entry, self._tracker = hass, entry, tracker
        self._alert_inputs, self._on_raised = alert_inputs, on_raised
        # Monotonic, so a clock correction cannot raise or hide anything and a resume from
        # suspend cannot raise anything.
        self._watch = ProblemWatch(started=time.monotonic())
        self._raised: Dict[str, Tuple[Tuple[str, str], ...]] = {}
        self._unsubscribe: Optional[Callable[[], None]] = None

    def _issue_id(self, key: str) -> str:
        # One set of issues per configured pilot.
        return f"{key}_{self._entry.entry_id}"

    @callback
    def start(self) -> None:
        self._unsubscribe = async_track_time_interval(self._hass, self.check, CHECK_INTERVAL)

    @callback
    def check(self, _now: Any = None) -> None:
        published = self._tracker.published
        pilot = published.get(PILOT_CONNECTED, (None, {}))[0]
        # The attribute does not exist until the engine has set itself up on its worker.
        last_health_check = getattr(self._tracker, "last_health_check", None)
        active = self._watch.evaluate(time.monotonic(), published.get(FEED_HEALTH), pilot == "on",
                                      last_health_check, self._alert_inputs() if self._alert_inputs else None)
        for key, placeholders in active.items():
            signature = tuple(sorted(placeholders.items()))
            if self._raised.get(key) == signature:
                continue
            new = key not in self._raised
            ir.async_create_issue(
                self._hass, DOMAIN, self._issue_id(key),
                is_fixable=False, is_persistent=False,
                severity=ir.IssueSeverity.ERROR if key in ERRORS else ir.IssueSeverity.WARNING,
                translation_key=key, translation_placeholders=placeholders or None,
                learn_more_url=f"{PROJECT_URL}#troubleshooting",
            )
            self._raised[key] = signature
            if new and self._on_raised is not None:
                self._on_raised(key)
        for key in [k for k in self._raised if k not in active]:
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id(key))
            del self._raised[key]

    @callback
    def pause(self) -> None:
        """Stop checking and leave what is raised alone. For Home Assistant stopping: the
        tracker is shut down then, and that must not be reported as a tracker that stopped."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    @callback
    def stop(self) -> None:
        """Stop checking and clear everything raised: an unloaded entry has no problems to
        report. Safe to call twice."""
        self.pause()
        for key in list(self._raised):
            ir.async_delete_issue(self._hass, DOMAIN, self._issue_id(key))
        self._raised.clear()
