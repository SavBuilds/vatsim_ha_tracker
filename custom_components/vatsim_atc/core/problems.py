"""Decides which problems are worth telling the user about, from what the engine publishes.

No Home Assistant imports. The integration turns each active problem into a Repairs issue
(issues.py); a problem that stops being active is cleared again. Every rule here used to be
a YAML automation in the alerts package.

A problem is reported only after its condition has held for a while, so a slow start or one
missed download does not raise anything.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

# Seconds a condition must hold before it is reported.
HOLD_FEED_UNREACHABLE = 600
HOLD_PROCESSING_ERROR = 300
HOLD_BOUNDARIES = 300
HOLD_NAVDATA = 300
# How old the last good download may be. Shorter while a flight is on, when it matters;
# longer otherwise, so a short internet outage overnight raises nothing.
STALE_CONNECTED_SECONDS = 180
STALE_IDLE_SECONDS = 900
# No publication at all from the engine for this long means it has stopped.
STALLED_SECONDS = 300

FEED_UNREACHABLE = "feed_unreachable"
FEED_STALE = "feed_stale"
TRACKER_STALLED = "tracker_stalled"
PROCESSING_ERROR = "processing_error"
BOUNDARIES_NOT_LOADED = "boundaries_not_loaded"
NAVDATA_NOT_LOADED = "navdata_not_loaded"
NAVDATA_EXPIRED = "navdata_expired"

ALL = (FEED_UNREACHABLE, FEED_STALE, TRACKER_STALLED, PROCESSING_ERROR, BOUNDARIES_NOT_LOADED,
       NAVDATA_NOT_LOADED, NAVDATA_EXPIRED)
# The tracker is not doing its job at all. Everything else is a degraded feature.
ERRORS = frozenset({FEED_UNREACHABLE, TRACKER_STALLED, PROCESSING_ERROR})

Health = Tuple[Any, Mapping[str, Any]]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text[:300] if text else fallback


class ProblemWatch:
    """Call evaluate() regularly. It remembers when each condition first became true."""

    def __init__(self, started: float) -> None:
        self._started = started
        self._since: Dict[str, float] = {}

    def _held(self, key: str, condition: bool, now: float, hold: float) -> bool:
        if not condition:
            self._since.pop(key, None)
            return False
        return now - self._since.setdefault(key, now) >= hold

    def evaluate(self, now: float, health: Optional[Health], pilot_connected: bool,
                 last_publish: Optional[float]) -> Dict[str, Dict[str, str]]:
        """Active problems, each with the text to fill into its message. Placeholders are kept
        to values that do not change every poll, so an issue is not rewritten constantly."""
        quiet_for = now - (last_publish if last_publish is not None else self._started)
        if quiet_for > STALLED_SECONDS:
            # What was last published is stale, so nothing else can be judged from it.
            return {TRACKER_STALLED: {}}
        if health is None:
            return {}                     # nothing published yet, and still inside the grace time
        _state, attrs = health
        active: Dict[str, Dict[str, str]] = {}

        last_success = _number(attrs.get("last_success_ts"))
        failures = _number(attrs.get("consecutive_failures"))
        if self._held(FEED_UNREACHABLE, last_success == 0 and failures > 0, now, HOLD_FEED_UNREACHABLE):
            active[FEED_UNREACHABLE] = {}
        limit = STALE_CONNECTED_SECONDS if pilot_connected else STALE_IDLE_SECONDS
        if self._held(FEED_STALE, last_success > 0 and now - last_success > limit, now, 0):
            active[FEED_STALE] = {}

        if self._held(PROCESSING_ERROR, attrs.get("timestamp_status") == "processing_error", now,
                      HOLD_PROCESSING_ERROR):
            active[PROCESSING_ERROR] = {}

        geometry = attrs.get("geometry_status")
        if self._held(BOUNDARIES_NOT_LOADED, geometry is not None and geometry != "ready", now, HOLD_BOUNDARIES):
            active[BOUNDARIES_NOT_LOADED] = {
                "error": _text(attrs.get("last_geometry_refresh_error"), "no error was recorded")}

        navdata = attrs.get("navdata_status")
        broken = navdata in ("configured_but_unloaded", "configured_but_empty") \
            or _number(attrs.get("navdata_load_issue_count")) > 0
        if self._held(NAVDATA_NOT_LOADED, broken, now, HOLD_NAVDATA):
            active[NAVDATA_NOT_LOADED] = {
                "status": _text(navdata, "unknown"),
                "error": _text(attrs.get("last_navdata_refresh_error"), "no error was recorded")}
        if self._held(NAVDATA_EXPIRED, navdata == "expired", now, 0):
            active[NAVDATA_EXPIRED] = {
                "cycle": _text(attrs.get("navdata_cycle"), "unknown"),
                "valid_until": _text(attrs.get("navdata_valid_until"), "an unknown date")}
        return active
