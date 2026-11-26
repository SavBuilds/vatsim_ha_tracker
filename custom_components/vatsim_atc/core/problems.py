"""Decides which problems are worth telling the user about, from what the engine publishes.

No Home Assistant imports. The integration turns each active problem into a Repairs issue
(issues.py); a problem that stops being active is cleared again. Every rule here used to be
a YAML automation in the alerts package.

A problem is reported only after its condition has held for a while, so a slow start or one
missed download does not raise anything.

Every time here is on the caller's monotonic clock (time.monotonic()), never the wall clock:
a clock correction must not raise or hide anything, and a resume from suspend must not raise
anything. On Linux that clock stands still while the machine is suspended, so time spent
suspended does not count toward a hold either.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

# Seconds a condition must hold before it is reported.
HOLD_FEED_UNREACHABLE = 600
HOLD_PROCESSING_ERROR = 300
HOLD_BOUNDARIES = 300
HOLD_NAVDATA = 300
# A first load that is still running and has reported no error is not a failure yet. Large
# files on a slow disk can take minutes, so that case waits much longer.
HOLD_STILL_LOADING = 1800
# How long since the last good download. Shorter while a flight is on, when it matters;
# longer otherwise, so a short internet outage overnight raises nothing.
STALE_CONNECTED_SECONDS = 180
STALE_IDLE_SECONDS = 900
# The engine finishes a health check on every poll, at most about a minute apart. None for
# this long means it has stopped, or can no longer report its own health.
STALLED_SECONDS = 300
# Days past the last day of the loaded AIRAC cycle before it is reported. The cycle ends at a
# UTC date line, which is the evening before in the Americas, and nobody replaces their files
# that minute: reporting at once is noise. 2 means the second UTC day after the last valid
# one, so at least 24 hours after the cycle ended. The engine's own status still reads
# "expired" from the first day, for anyone who wants to show it.
NAVDATA_EXPIRED_GRACE_DAYS = 2
# A METAR was wanted and both sources have failed for this long. One failed refresh is not it.
HOLD_METAR = 900
# The entity that holds speech is missing or unavailable. Speech is silently held meanwhile.
HOLD_MUTE_GATE = 900

FEED_UNREACHABLE = "feed_unreachable"
FEED_STALE = "feed_stale"
TRACKER_STALLED = "tracker_stalled"
PROCESSING_ERROR = "processing_error"
BOUNDARIES_NOT_LOADED = "boundaries_not_loaded"
NAVDATA_NOT_LOADED = "navdata_not_loaded"
NAVDATA_EXPIRED = "navdata_expired"
METAR_FAILING = "metar_failing"
POSITION_NOT_RESTORED = "position_not_restored"
MUTE_GATE_UNAVAILABLE = "mute_gate_unavailable"
ALERT_DELIVERY_FAILED = "alert_delivery_failed"

ALL = (FEED_UNREACHABLE, FEED_STALE, TRACKER_STALLED, PROCESSING_ERROR, BOUNDARIES_NOT_LOADED,
       NAVDATA_NOT_LOADED, NAVDATA_EXPIRED, METAR_FAILING, POSITION_NOT_RESTORED, MUTE_GATE_UNAVAILABLE,
       ALERT_DELIVERY_FAILED)
# What a push for each problem says. The full text is in the repair itself.
PUSH_TITLES = {
    FEED_UNREACHABLE: "VATSIM feed has not been reachable since the tracker started",
    FEED_STALE: "VATSIM feed is not updating",
    TRACKER_STALLED: "VATSIM tracker has stopped",
    PROCESSING_ERROR: "VATSIM tracker cannot process the feed",
    BOUNDARIES_NOT_LOADED: "VATSIM sector boundaries are not loaded",
    NAVDATA_NOT_LOADED: "VATSIM navigation data did not load",
    NAVDATA_EXPIRED: "VATSIM navigation data has expired",
    METAR_FAILING: "VATSIM METAR lookups are failing",
    POSITION_NOT_RESTORED: "VATSIM saved aircraft position could not be read",
    MUTE_GATE_UNAVAILABLE: "VATSIM speech is held: the hold entity is unavailable",
    ALERT_DELIVERY_FAILED: "A VATSIM alert could not be delivered",
}
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
        self._success_seen: Tuple[float, float] = (0.0, started)   # (last_success_ts, when it changed)
        self._active: Dict[str, Dict[str, str]] = {}
        self._quiet_before = False

    def _held(self, key: str, condition: bool, now: float, hold: float) -> bool:
        if not condition:
            self._since.pop(key, None)
            return False
        return now - self._since.setdefault(key, now) >= hold

    def evaluate(self, now: float, health: Optional[Health], pilot_connected: bool,
                 last_health_check: Optional[float],
                 alerts: Optional[Mapping[str, Any]] = None) -> Dict[str, Dict[str, str]]:
        """Active problems, each with the text to fill into its message. Placeholders are kept
        to values that do not change every poll, so an issue is not rewritten constantly.

        now and last_health_check are monotonic seconds. last_health_check is when the engine
        last finished working out its health, whether or not anything changed; other entities
        being published does not count, or a fault in the health report itself would be hidden.

        alerts is what the alert sender knows and the engine does not: "gate" is
        (entity id, its state) when an entity holds speech, and "delivery_error" is the text
        of the last push or speech call that failed, until one works again.
        """
        outside = self._outside(now, alerts or {})
        quiet_for = now - (last_health_check if last_health_check is not None else self._started)
        quiet, quiet_before = quiet_for > STALLED_SECONDS, self._quiet_before
        self._quiet_before = quiet
        if quiet and not quiet_before:
            # The caller reads the health and the time of its last check one after the other,
            # from another thread, and can catch the moment between the two being written.
            # One quiet reading changes nothing; the next check, 30 s on, settles it.
            return {**self._active, **outside}
        if quiet:
            # What was last published is stale, so nothing can be judged from it. Problems
            # already reported stay as they are: clearing them here and raising them again
            # afterwards would notify twice for one fault.
            return {**self._active, **outside, TRACKER_STALLED: {}}
        if health is None:
            return outside                # nothing published yet, and still inside the grace time
        _state, attrs = health
        active: Dict[str, Dict[str, str]] = {}

        last_success = _number(attrs.get("last_success_ts"))
        failures = _number(attrs.get("consecutive_failures"))
        if self._held(FEED_UNREACHABLE, last_success == 0 and failures > 0, now, HOLD_FEED_UNREACHABLE):
            active[FEED_UNREACHABLE] = {}
        # The age of the last good download is measured from when this watch saw the value
        # change, not by subtracting a wall-clock timestamp.
        if last_success != self._success_seen[0]:
            self._success_seen = (last_success, now)
        limit = STALE_CONNECTED_SECONDS if pilot_connected else STALE_IDLE_SECONDS
        if last_success > 0 and now - self._success_seen[1] > limit:
            active[FEED_STALE] = {}

        if self._held(PROCESSING_ERROR, attrs.get("timestamp_status") == "processing_error", now,
                      HOLD_PROCESSING_ERROR):
            active[PROCESSING_ERROR] = {}

        geometry = attrs.get("geometry_status")
        geometry_error = _text(attrs.get("last_geometry_refresh_error"), "")
        missing = geometry is not None and geometry != "ready"
        # Both are evaluated every time, so each keeps its own start time.
        failed = self._held(BOUNDARIES_NOT_LOADED, missing and bool(geometry_error), now, HOLD_BOUNDARIES)
        slow = self._held(BOUNDARIES_NOT_LOADED + "_slow", missing, now, HOLD_STILL_LOADING)
        if failed or slow:
            active[BOUNDARIES_NOT_LOADED] = {"error": geometry_error or "no error was recorded"}

        navdata = attrs.get("navdata_status")
        navdata_error = _text(attrs.get("last_navdata_refresh_error"), "")
        # A finished load that came out empty or with files missing, or any load that raised.
        # The last covers a failed reload too, where the older data stays in use and the
        # status still reads active.
        broken = navdata == "configured_but_empty" or bool(navdata_error) \
            or _number(attrs.get("navdata_load_issue_count")) > 0
        failed = self._held(NAVDATA_NOT_LOADED, broken, now, HOLD_NAVDATA)
        slow = self._held(NAVDATA_NOT_LOADED + "_slow", navdata == "configured_but_unloaded", now,
                          HOLD_STILL_LOADING)
        if failed or slow:
            active[NAVDATA_NOT_LOADED] = {
                "status": _text(navdata, "unknown"),
                "error": navdata_error or "no error was recorded"}
        if navdata == "expired" and _number(attrs.get("navdata_days_expired")) >= NAVDATA_EXPIRED_GRACE_DAYS:
            active[NAVDATA_EXPIRED] = {
                "cycle": _text(attrs.get("navdata_cycle"), "unknown"),
                "valid_until": _text(attrs.get("navdata_valid_until"), "an unknown date")}
        if self._held(METAR_FAILING, attrs.get("metar_status") == "error", now, HOLD_METAR):
            active[METAR_FAILING] = {"error": _text(attrs.get("last_metar_error"), "no error was recorded")}
        restore_error = _text(attrs.get("last_aircraft_restore_error"), "")
        if restore_error:
            active[POSITION_NOT_RESTORED] = {"error": restore_error}
        self._active = active
        return {**active, **outside}

    def _outside(self, now: float, alerts: Mapping[str, Any]) -> Dict[str, Dict[str, str]]:
        """Problems that do not come from the engine's health, so a stopped engine neither
        hides nor freezes them."""
        found: Dict[str, Dict[str, str]] = {}
        gate = alerts.get("gate")
        broken = gate is not None and gate[1] not in ("on", "off")
        if self._held(MUTE_GATE_UNAVAILABLE, broken, now, HOLD_MUTE_GATE):
            found[MUTE_GATE_UNAVAILABLE] = {"entity": _text(gate[0], "unknown"), "state": _text(gate[1], "missing")}
        delivery_error = _text(alerts.get("delivery_error"), "")
        if delivery_error:
            found[ALERT_DELIVERY_FAILED] = {"error": delivery_error}
        return found
