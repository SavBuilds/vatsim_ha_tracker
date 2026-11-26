"""Decides when the three flight alerts are due, and what they say.

No Home Assistant imports. alerts.py runs these rules on what the engine publishes and does
the sending. Every rule here used to be a YAML automation in the alerts package, and behaves
the same way.

A state is the pair the engine publishes: (state, attributes). None means not published yet.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

State = Optional[Tuple[Any, Mapping[str, Any]]]

COVERAGE = "binary_sensor.vatsim_atc_coverage"
NEXT_SECTOR = "sensor.vatsim_next_sector"
DESTINATION = "sensor.vatsim_destination"
PILOT_CONNECTED = "binary_sensor.vatsim_pilot_connected"
SOURCES = (COVERAGE, NEXT_SECTOR, DESTINATION)

CONTROLLER_ONLINE = "controller_online"
NEXT_CONTROLLER = "next_controller"
ATIS_CHANGE = "atis_change"
KINDS = (CONTROLLER_ONLINE, NEXT_CONTROLLER, ATIS_CHANGE)

# A controller who logs on and straight off again is not announced.
CONTROLLER_ONLINE_CONFIRM_SECONDS = 20

MODE_DISABLED = "disabled"
MODE_PUSH_AND_SPEAKER = "push_and_speaker"
MODE_PUSH_ONLY = "push_only"
MODE_SPEAKER_ONLY = "speaker_only"
MODES = (MODE_DISABLED, MODE_PUSH_AND_SPEAKER, MODE_PUSH_ONLY, MODE_SPEAKER_ONLY)
# A new install has one push target, the sidebar notice, and no speakers.
DEFAULT_MODE = MODE_PUSH_ONLY

_NO_CALLSIGN = (None, "", "unknown")
# Lookahead states that name no controller ahead.
_NEXT_SECTOR_QUIET = frozenset({
    None, "unknown", "unavailable", "none", "matches", "skipped_low_speed", "skipped_unstable_heading",
    "skipped_insufficient_history"})
_ATIS_STATES = ("in_range", "departure", "selected")
_PHONETIC = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta", "E": "Echo", "F": "Foxtrot", "G": "Golf",
    "H": "Hotel", "I": "India", "J": "Juliett", "K": "Kilo", "L": "Lima", "M": "Mike", "N": "November",
    "O": "Oscar", "P": "Papa", "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango", "U": "Uniform",
    "V": "Victor", "W": "Whiskey", "X": "X-ray", "Y": "Yankee", "Z": "Zulu"}

Message = Tuple[str, str]   # (title, text)


def push_enabled(mode: str) -> bool:
    return mode in (MODE_PUSH_AND_SPEAKER, MODE_PUSH_ONLY)


def speech_enabled(mode: str) -> bool:
    return mode in (MODE_PUSH_AND_SPEAKER, MODE_SPEAKER_ONLY)


def _parts(state: State) -> Tuple[Any, Mapping[str, Any]]:
    return (None, {}) if state is None else (state[0], state[1] or {})


def controller_online_started(old: State, new: State) -> bool:
    """Coverage has just become controlled. The engine marks a position restored after a
    restart as "restored", so this is not true for a controller who was already there."""
    _, before = _parts(old)
    _, after = _parts(new)
    return after.get("last_change_reason") == "became_controlled" \
        and before.get("last_change_reason") != "became_controlled"


def controller_online_message(coverage: State) -> Optional[Message]:
    """The announcement, if the controller is still there when the wait is over."""
    state, attrs = _parts(coverage)
    callsign = attrs.get("controller_callsign")
    if state != "on" or callsign in _NO_CALLSIGN:
        return None
    return "VATSIM ATC Online", f"Attention. ATC is now online: {callsign}."


def next_controller_message(old: State, new: State) -> Optional[Message]:
    if new is None:
        return None
    before_state, before = _parts(old)
    state, attrs = _parts(new)
    callsign = attrs.get("controller_callsign")
    if before_state == state and before.get("controller_callsign") == callsign:
        return None
    if state in _NEXT_SECTOR_QUIET or callsign in _NO_CALLSIGN:
        return None
    return "VATSIM Next ATC", f"Heads up. Next likely controller: {callsign}."


def atis_change_message(old: State, new: State, pilot_connected: bool) -> Optional[Message]:
    """A new information letter for the same airport, during a flight. The first letter seen
    for an airport is not a change."""
    if old is None or new is None or not pilot_connected:
        return None
    before_state, before = _parts(old)
    state, attrs = _parts(new)
    old_code, code = before.get("atis_code"), attrs.get("atis_code")
    if before_state != state or state not in _ATIS_STATES:
        return None
    if before.get("airport") != attrs.get("airport") or attrs.get("session") != "connected":
        return None
    if old_code in (None, "") or code in (None, "") or old_code == code:
        return None
    letter = str(code).upper()
    return "VATSIM ATIS", f"{attrs.get('airport')} ATIS is now information {_PHONETIC.get(letter, letter)}."


def mute_gate_holds(gate_state: Optional[str]) -> bool:
    """Speech is held unless the gate entity reads exactly off. None means no gate is set.
    A gate that is missing or unavailable holds speech too: the safe direction."""
    return gate_state is not None and gate_state != "off"


def mute_gate_broken(gate_state: Optional[str]) -> bool:
    return gate_state is not None and gate_state not in ("on", "off")
