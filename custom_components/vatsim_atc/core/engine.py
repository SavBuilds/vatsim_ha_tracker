"""The tracker engine: everything the integration knows about VATSIM, with no Home Assistant in it.

Each poll it reads the public VATSIM data feed, finds one pilot by CID, and works out:

  - presence, debounced, so one missed poll is not a disconnect;
  - the controller covering the aircraft now, from VAT-Spy centre and FSS boundaries and,
    optionally, SimAware approach and departure boundaries;
  - the next controlled sector along the projected track, with a distance and an ETA;
  - the filed route as a line, resolved against navigation data the user supplies, with
    North Atlantic Tracks expanded from natTrak;
  - the ATIS at the departure, the arrival or a chosen airport, or its METAR when no ATIS
    is online;
  - a GeoJSON layer of every online controller's boundary, and the session's track.

Results are published through Host.set_state as (entity id, state, attributes). The entity
ids used here are internal keys; the integration maps them to its own entities.

Design notes that are easy to break:

  - Boundaries, navdata and NAT tracks are loaded on worker threads into immutable snapshots
    and swapped in on the host loop with run_in(..., 0). The poll never sees a half-loaded set.
  - A failed load keeps the previous snapshot and records the error on the health state. It
    never replaces good data with an empty set.
  - Everything is fail-soft and reports through the feed-health state, so a watchdog can see
    what a log line would hide.
  - Geometry is geo.py: spherical distances and plain ray casting. No compiled dependency.

The engine is a mixin. See Host below for what it needs from whatever runs it.
"""

import glob
import json
import os
import random
import re
import threading
import time
import traceback
import uuid
from collections import ChainMap, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from .. import geo


class FetchError(Exception):
    """A download failed. Raised by Host.http_get for a network error, a timeout or an HTTP error status."""

    def __init__(self, message: str, kind: str = "FetchError", is_timeout: bool = False) -> None:
        super().__init__(message)
        self.kind = kind              # class name of the underlying error, kept for the health attributes
        self.is_timeout = is_timeout  # a read timeout, which the feed check reports separately


class Host:
    """What the engine needs from whatever runs it. The engine is a mixin: combine it with a
    concrete host, engine first, so the engine's set_state wraps the host's.

        class Tracker(VatsimAtcEngine, MyHost): ...

    Threading contract: log, run_in and http_get may be called from worker threads. Everything
    else is called only from the host's own loop, and callbacks given to run_in, run_every and
    listen_state must be run on that loop."""

    args: Dict[str, Any]

    def log(self, message: str, level: str = "INFO", **kwargs: Any) -> None:
        raise NotImplementedError

    def set_state(self, entity_id: str, **kwargs: Any) -> Any:
        """Publish state= and attributes= for one entity."""
        raise NotImplementedError

    def get_state(self, entity_id: Optional[str] = None, **kwargs: Any) -> Any:
        raise NotImplementedError

    def render_template(self, template: str, **kwargs: Any) -> Any:
        """Rendered text, or None when the host cannot answer."""
        raise NotImplementedError

    def call_service(self, service: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def run_in(self, callback: Any, delay: float, **kwargs: Any) -> Any:
        """Call callback(kwargs) on the host loop after delay seconds. Delay 0 is a thread hand-off."""
        raise NotImplementedError

    def run_every(self, callback: Any, start: Any, interval: float, **kwargs: Any) -> Any:
        raise NotImplementedError

    def listen_state(self, callback: Any, entity_id: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def datetime(self) -> datetime:
        raise NotImplementedError

    def http_get(self, url: str, timeout: float, headers: Optional[Dict[str, str]] = None) -> str:
        """Body text of a successful GET. Raises FetchError otherwise. Blocking; called from threads
        and, for the feed, from the poll."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

VATSIM_FEED_URL = "https://data.vatsim.net/v3/vatsim-data.json"

FACILITY_FSS = 1
FACILITY_APP_DEP = 5
FACILITY_CTR = 6

DEFAULT_POLL_INTERVAL_SECONDS = 18
DEFAULT_LOOKAHEAD_MINUTES = 10.0
DEFAULT_MIN_GROUNDSPEED_KT = 30.0
DEFAULT_ALTITUDE_HYSTERESIS_FT = 500.0
DEFAULT_PRESENCE_ABSENT_AFTER_MISSES = 3
DEFAULT_STALE_FEED_MULTIPLIER = 2.0
DEFAULT_STALE_FEED_GRACE_SECONDS = 45.0
DEFAULT_APP_DEP_ALTITUDE_GATE_FT = 18000.0
DEFAULT_EXPECTED_FEED_REGEN_SECONDS = 15.0

MAX_CONSECUTIVE_FEED_FAILURES = 3
MAX_BACKOFF_SECONDS = 60.0
POLL_JITTER_SECONDS = 2.0

HEADING_STABILITY_WINDOW = 3
HEADING_STABILITY_MAX_DELTA_DEG = 20.0
ALTITUDE_TREND_WINDOW = 3
DEFAULT_GEOMETRY_REFRESH_INTERVAL_DAYS = 7
DEFAULT_NAVDATA_REFRESH_INTERVAL_DAYS = 30
# Files whose change triggers a navdata reload, and how often to look.
NAVDATA_SOURCE_FILES = ("cycle.json", "cycle_info.txt", "earth_fix.dat", "earth_nav.dat",
                        "earth_awy.dat", "earth_aptmeta.dat")
NAVDATA_CHANGE_CHECK_SECONDS = 3600
# A load that failed at start is retried on this schedule until it succeeds. Without it the
# next attempt would be the scheduled refresh, which for boundaries is a week away.
STARTUP_RETRY_FIRST_SECONDS = 120
STARTUP_RETRY_INTERVAL_SECONDS = 300
# A boundary download is refused when fewer than this share of the distinct boundaries the
# FIR list refers to are present with an outline the coverage code can use. Half is a
# deliberately loose bar: it tolerates a FIR list that names some boundaries the file does
# not carry, and still catches a truncated download or one whose shapes are missing.
MIN_USABLE_BOUNDARY_SHARE = 0.5
# Free text from the feed (an ATIS, a filed route) is cut to this many bytes as written in
# JSON. Normal values are far shorter. An oversized one would push a state's attributes past
# the recorder's 16 KB limit, and the recorder then stores none of that state's attributes.
MAX_FEED_TEXT_BYTES = 4000
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG",
                                       "SEP", "OCT", "NOV", "DEC"), start=1)}
DEBOUNCE_THRESHOLD = 2  # consecutive polls required before publishing an identity change
DEFAULT_TRACK_HISTORY_MAX_POINTS = 300
DEFAULT_TRACK_HISTORY_MIN_DISTANCE_NM = 1.0
DEFAULT_TRACK_HISTORY_MIN_SECONDS = 30.0
BOUNDARY_GEOJSON_SIMPLIFY_TOLERANCE_DEG = 0.01
MAX_AIRWAY_BFS_VISITED_NODES = 2000
ROUTE_TOKEN_SKIP_WORDS = {"DCT"}

# ICAO field 15 speed/level group, e.g. N0495F430, M083F350, K0900S1010.
# Used to decide whether the text after a slash in a route token is a
# speed/level change (strippable) or part of the identifier itself
# (oceanic lat/lon shorthand such as 59/20, which must be preserved).
ICAO_SPEED_LEVEL_RE = re.compile(r"^[NMK]\d{3,4}[FSAM]\d{3,4}$")

# NAT tracks. natTrak publishes each track's routeing as a plain
# space-separated string, not as coordinate objects, so the oceanic
# shorthand points have to be converted here. Format is latitude north
# over longitude west: "56/20" is 56N 020W. Named entry/exit points
# (RESNO, NEEKO, ...) are ordinary fixes already present in
# earth_fix.dat and are looked up normally.
NAT_TRACKS_URL = "https://nattrak.vatsim.net/api/tracks"
NAT_TRACK_FETCH_TIMEOUT_SECONDS = 15
# Tracks reissue roughly twice daily and the westbound/eastbound
# changeover matters, so this CANNOT ride navdata_refresh_interval_days
# (30 by default) - it needs its own, much shorter, timer.
DEFAULT_NAT_TRACK_REFRESH_INTERVAL_MINUTES = 30
NAT_OCEANIC_POINT_RE = re.compile(r"^(\d{1,2})/(\d{1,3})$")

AIRCRAFT_TRACKER_ENTITY_ID = "device_tracker.vatsim_aircraft"
SESSION_TRACK_ENTITY_ID = "sensor.vatsim_session_track"
# A move this large and this fast between track points is a respawn, not flight, so the map
# line breaks there. 2000 kt is above 4x sim rate at cruise; a false break only leaves a gap.
TRACK_JUMP_MIN_NM = 5.0
TRACK_JUMP_KT = 2000.0
# Speed over a long gap proves nothing, 12 nm in 10 min reads as 72 kt.
# 120 s is above the 5 s recording minimum plus a few missed 18 s polls.
TRACK_GAP_SECONDS = 120.0
EMPTY_TRACK_GEOJSON = json.dumps({"type": "FeatureCollection", "features": []})
ONLINE_ATC_BOUNDARIES_ENTITY_ID = "sensor.vatsim_online_atc_boundaries"
FLIGHT_ROUTE_ENTITY_ID = "sensor.vatsim_flight_route"
# Destination distance and ATIS, from the feed this app already fetches.
DESTINATION_ENTITY_ID = "sensor.vatsim_destination"
DEFAULT_DESTINATION_RANGE_NM = 100.0
# On the ground within this distance of the filed departure, below the takeoff speed, the
# card shows the departure ATIS. The first poll above the takeoff speed latches "airborne".
DEPARTURE_NEAR_NM = 5.0
TAKEOFF_GROUNDSPEED_KT = 40.0
# One fast poll used to latch "airborne" for the session. Now speed must hold
# for TAKEOFF_CONFIRM_POLLS and be backed by a climb above the lowest altitude seen, or by being
# past DEPARTURE_NEAR_NM. A fast taxi or a rejected takeoff stays on the departure ATIS. Stopped
# near the filed departure for LANDED_CONFIRM_POLLS releases the latch (returned to the gate).
TAKEOFF_CONFIRM_POLLS = 2
TAKEOFF_CLIMB_FT = 300.0
LANDED_GROUNDSPEED_KT = 5.0
LANDED_CONFIRM_POLLS = 5
# A typed-in airport overrides the automatic choice, connected or not. Blank = automatic.
ATIS_AIRPORT_OVERRIDE_ENTITY_ID = "input_text.vatsim_atis_airport"
# Leaves range only this far beyond it, so a holding pattern at the edge does not flap the card.
DESTINATION_RANGE_HYSTERESIS_NM = 5.0
# No ATIS online for the airport the card shows, so show its METAR.
# VATSIM's own METAR service first (the weather VATSIM ATIS and controllers work from), then
# aviationweather.gov, which also supplies a flight category. Fetched off the main thread, at
# most once per airport per metar_refresh_minutes, and only while that airport has no ATIS.
METAR_VATSIM_URL = "https://metar.vatsim.net/{icao}"
METAR_AWC_URL = "https://aviationweather.gov/api/data/metar?ids={icao}&format=json"
METAR_FETCH_TIMEOUT_SECONDS = 10
DEFAULT_METAR_REFRESH_MINUTES = 10.0
# Hourly reports plus one missed report, with margin. An older METAR is dropped rather than shown.
METAR_MAX_AGE_SECONDS = 90 * 60
# A fetch thread that never handed back (it cannot outlive two 10 s timeouts) is retried after this.
METAR_INFLIGHT_STALE_SECONDS = 120
METAR_HEADERS = {"User-Agent": "vatsim_ha_tracker (+https://github.com/SavBuilds/vatsim_ha_tracker)"}
METAR_TIME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})Z$")
METAR_LAYER_RE = re.compile(r"^(BKN|OVC|VV)(\d{3})")
METAR_SM_RE = re.compile(r"^([PM])?(\d+)?(?:(\d+)/(\d+))?SM$")
METAR_METERS_RE = re.compile(r"^(\d{4})(NDV)?$")
FLIGHT_CATEGORIES = ("VFR", "MVFR", "IFR", "LIFR")

# The last aircraft position is saved to a file so the map has a position after a restart,
# before the pilot next connects. The host decides where the file lives (_last_aircraft_path).
LAST_AIRCRAFT_STATE_FILENAME = "vatsim_last_aircraft.json"
# Write cadence while flying; clears and terminate() write immediately.
LAST_AIRCRAFT_PERSIST_MIN_SECONDS = 60
# One restore per HA restart is expected. A second within this window means
# the republish is not sticking, so it is logged at WARNING.
RESTORE_REPEAT_WARN_SECONDS = 600

# Publish verification, for hosts whose set_state can fail without saying so (a host that
# writes states over an API from outside Home Assistant). Every poll, one template render reads
# the publication token of every published entity; one that is missing, or holds a token other
# than the last write, is republished from _published_states. A host whose set_state cannot
# fail silently overrides _reconcile_published_states with a no-op, as the integration does.
# The token involves no clock. It changes only when the content changes, so a repeat write
# stays an unchanged state. A string, because some hosts drop None, False and 0.
# The state is compared too, so an edit that keeps the token (Developer Tools
# pre-fills the attributes) is repaired. Attributes are not compared: an edit to one alone stays
# until the next change, because reading the geojson attributes every poll costs more than that risk.
PUBLISH_TOKEN_ATTR = "pub_token"
PUBLISH_VERIFY_TEMPLATE = (
    "{%- set ns = namespace(out=[]) -%}"
    "{%- for s in expand(ids) -%}"
    "{%- set ns.out = ns.out + [[s.entity_id, s.attributes.get('pub_token'), s.state]] -%}"
    "{%- endfor -%}"
    "{{ ns.out }}"
)
# Unconfirmed after this many republishes: reported in HA (persistent notification and the
# publish_unverified attributes on sensor.vatsim_feed_health).
PUBLISH_FAILING_AFTER_ATTEMPTS = 3
PUBLISH_NOTIFICATION_ID = "vatsim_publish_failing"

DEFAULT_VATSPY_BOUNDARIES_URL = (
    "https://raw.githubusercontent.com/vatsimnetwork/vatspy-data-project/master/Boundaries.geojson"
)
DEFAULT_VATSPY_DAT_URL = (
    "https://raw.githubusercontent.com/vatsimnetwork/vatspy-data-project/master/VATSpy.dat"
)
DEFAULT_SIMAWARE_BOUNDARIES_PATH = None
DEFAULT_NAVDATA_PATH = None


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

def parse_metar_obs_time(raw: str, now_utc: datetime) -> Optional[float]:
    """Epoch seconds of a METAR's DDHHMMZ group. The group has no month, so take the latest
    matching date that is not in the future (1 h slack for clock skew)."""
    for token in raw.split()[:4]:
        m = METAR_TIME_RE.match(token)
        if not m:
            continue
        day, hh, mm = (int(g) for g in m.groups())
        if not (1 <= day <= 31 and hh < 24 and mm < 60):
            return None
        year, month = now_utc.year, now_utc.month
        for _ in range(3):
            try:
                dt = datetime(year, month, day, hh, mm, tzinfo=timezone.utc)
            except ValueError:
                dt = None  # day 31 in a 30-day month: try the month before
            if dt is not None and dt.timestamp() <= now_utc.timestamp() + 3600:
                return dt.timestamp()
            month -= 1
            if month < 1:
                month, year = 12, year - 1
        return None
    return None


def metar_flight_category(raw: str) -> Optional[str]:
    """FAA flight category from a raw METAR: LIFR ceiling < 500 ft or vis < 1 SM, IFR < 1000 /
    < 3, MVFR <= 3000 / <= 5, else VFR. Ceiling is the lowest BKN, OVC or VV layer. None when
    the visibility cannot be read, rather than guessing from the ceiling alone."""
    tokens = raw.split()
    if "RMK" in tokens:
        tokens = tokens[:tokens.index("RMK")]
    vis_sm: Optional[float] = None
    ceiling_ft: Optional[float] = None
    for i, tok in enumerate(tokens):
        if tok == "CAVOK":
            vis_sm = 10.0
            continue
        if vis_sm is None:
            m = METAR_SM_RE.match(tok)
            if m and (m.group(2) or m.group(3)):
                # One token holds either a whole number (10SM, P6SM) or a fraction (1/2SM, M1/4SM).
                vis = float(m.group(2)) if m.group(2) else float(m.group(3)) / max(float(m.group(4)), 1.0)
                # "1 1/2SM": the whole number is the previous token.
                if m.group(3) and i > 0 and tokens[i - 1].isdigit() and len(tokens[i - 1]) == 1:
                    vis += float(tokens[i - 1])
                vis_sm = vis
                continue
            m = METAR_METERS_RE.match(tok)
            # Only after the time group, so a 4-digit token in the header is not read as visibility.
            if m and i >= 2:
                meters = int(m.group(1))
                vis_sm = 10.0 if meters >= 9999 else meters / 1609.344
                continue
        m = METAR_LAYER_RE.match(tok)
        if m:
            height = int(m.group(2)) * 100.0
            ceiling_ft = height if ceiling_ft is None else min(ceiling_ft, height)
    if vis_sm is None:
        return None
    ceiling = ceiling_ft if ceiling_ft is not None else float("inf")
    if ceiling < 500 or vis_sm < 1:
        return "LIFR"
    if ceiling < 1000 or vis_sm < 3:
        return "IFR"
    if ceiling <= 3000 or vis_sm <= 5:
        return "MVFR"
    return "VFR"


class FeedHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class PresenceState(str, Enum):
    UNKNOWN = "unknown"
    PRESENT = "present"
    SUSPECT_MISSING = "suspect_missing"
    ABSENT = "absent"


class NextSectorState(str, Enum):
    DIFFERS = "differs"
    MATCHES = "matches"
    SKIPPED_LOW_SPEED = "skipped_low_speed"
    SKIPPED_UNSTABLE_HEADING = "skipped_unstable_heading"
    SKIPPED_INSUFFICIENT_HISTORY = "skipped_insufficient_history"


class NextResultKind(str, Enum):
    DIFFERENT_SECTOR = "different_sector"
    PROJECTED_UNCONTROLLED = "projected_uncontrolled"
    SAME_SECTOR = "same_sector"
    STILL_UNCONTROLLED = "still_uncontrolled"
    SKIPPED_LOW_SPEED = "skipped_low_speed"
    SKIPPED_UNSTABLE_HEADING = "skipped_unstable_heading"
    SKIPPED_INSUFFICIENT_HISTORY = "skipped_insufficient_history"


class Mode(str, Enum):
    CURRENT = "current"
    NEXT = "next"


class RouteResolutionStatus(str, Enum):
    OK = "ok"
    NO_FLIGHT_PLAN = "no_flight_plan"
    NAVDATA_UNAVAILABLE = "navdata_unavailable"
    UNRESOLVED = "unresolved"  # route present, navdata present, but zero points resolved


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------

class Point(NamedTuple):
    """A position in GeoJSON order: x is longitude, y is latitude."""
    x: float
    y: float


def _boundary_or_raise(geometry: Any) -> "geo.Boundary":
    boundary = geo.boundary_from_geojson(geometry)
    if boundary is None:
        raise ValueError("not a usable Polygon or MultiPolygon")
    return boundary


@dataclass(frozen=True)
class SectorIdentity:
    """Debounce/identity key: (mode, controller_callsign, polygon_id)."""
    mode: Mode
    controller_callsign: Optional[str]
    polygon_id: Optional[str]

    def as_tuple(self) -> Tuple[str, Optional[str], Optional[str]]:
        return (self.mode.value, self.controller_callsign, self.polygon_id)


@dataclass
class ResolvedCoverage:
    identity: SectorIdentity
    frequency: Optional[str] = None
    facility: Optional[str] = None  # "CTR" / "FSS" / "APP" / "DEP"
    geometry: Any = None


@dataclass(frozen=True)
class CtrFeature:
    fir_id: str
    prefix: str
    geometry: Any


@dataclass(frozen=True)
class TraconFeature:
    polygon_id: str
    prefixes: Tuple[str, ...]
    suffix: str  # "APP" or "DEP"
    geometry: Any


@dataclass(frozen=True)
class VatspyRawData:
    prefix_to_boundary_id: Dict[str, Tuple[str, ...]]
    boundary_features_by_id: Dict[str, Any]  # raw GeoJSON feature dicts


@dataclass(frozen=True)
class GeometrySnapshot:
    version: int
    ctr_raw: VatspyRawData
    tracon_features: Tuple[TraconFeature, ...]
    loaded_at: float
    simaware_load_issue_count: int = 0


@dataclass(frozen=True)
class NavDataSnapshot:
    """An immutable snapshot of parsed local navdata (X-Plane 1200/1100/
    1210 format earth_fix.dat / earth_nav.dat / earth_awy.dat /
    earth_aptmeta.dat), built entirely off the main thread and handed
    back for a single atomic reference swap (self._navdata = new_snapshot),
    mirroring GeometrySnapshot's pattern exactly.

    fix_coords maps a bare waypoint/navaid identifier to a LIST of
    (lat, lon) candidates rather than a single value: identifiers are NOT
    globally unique across the full worldwide dataset, so ambiguity is
    preserved here and resolved downstream at route-parse time using
    proximity to the last-known point on the route being built (see
    _nearest_coord_for_ident) rather than silently picking whichever
    entry happened to load last.

    airport_coords follows the identical list-of-candidates shape as
    fix_coords, sourced from earth_aptmeta.dat via its OWN dedicated
    parser (_parse_navdata_airport_file) - that file's column layout is
    ident-first (ident, region, lat, lon, ...), not lat/lon-first like
    earth_fix.dat/earth_nav.dat, so it cannot reuse
    _parse_navdata_coord_file.

    airway_graph maps an airway name (e.g. "Q436") to an undirected
    adjacency dict of {ident: [neighbor_ident, ...]}. Direction
    restrictions present in the source data are deliberately not modeled
    - this graph is used only to draw a display line for situational
    awareness, not to validate or file a legal route.
    """
    version: int
    fix_coords: Dict[str, Tuple[Tuple[float, float], ...]]
    airport_coords: Dict[str, Tuple[Tuple[float, float], ...]]
    airway_graph: Dict[str, Dict[str, Tuple[str, ...]]]
    loaded_at: float
    load_issue_count: int = 0
    # AIRAC cycle from cycle.json / cycle_info.txt in navdata_path (None when
    # neither is readable), and the newest mtime of the source files at load time,
    # which _check_navdata_changed compares against.
    cycle: Optional[str] = None
    valid_until: Optional[str] = None
    source_mtime: float = 0.0


@dataclass
class NatTrackSnapshot:
    """An immutable snapshot of the currently in-force North Atlantic
    Tracks, kept SEPARATE from NavDataSnapshot on purpose: local navdata
    is a ~250k-row disk parse refreshed on an AIRAC cadence (30 days by
    default), whereas tracks reissue about twice daily. Folding them
    into one snapshot would mean either re-parsing every fix hourly or
    serving expired tracks for a month.

    fix_coords holds ONLY the oceanic shorthand points synthesized from
    each routeing string (56/20 -> 56N 020W). Named entry/exit points
    are deliberately not duplicated here; they resolve against the main
    navdata snapshot so that existing proximity disambiguation applies.

    airway_graph keys are the filed token form, "NAT" + identifier
    (NATA, NATD, ...), so that _resolve_route_to_points treats a track
    exactly as it already treats a named airway: fix, path, fix.

    valid_until is the EARLIEST valid_to across loaded tracks, i.e. the
    time at which this snapshot starts being partially wrong.
    """
    version: int
    fix_coords: Dict[str, Tuple[Tuple[float, float], ...]]
    airway_graph: Dict[str, Dict[str, Tuple[str, ...]]]
    track_ids: Tuple[str, ...]
    fetched_at: float
    valid_until: Optional[float] = None


@dataclass
class DebounceTracker:
    last_raw: Any = None
    consecutive: int = 0
    published: Any = None

    def offer(self, raw_value: Any, threshold: int = DEBOUNCE_THRESHOLD) -> bool:
        if raw_value == self.last_raw:
            self.consecutive += 1
        else:
            self.last_raw = raw_value
            self.consecutive = 1
        should_publish = self.consecutive >= threshold and raw_value != self.published
        if should_publish:
            self.published = raw_value
        return should_publish


# --------------------------------------------------------------------------
# Main app
# --------------------------------------------------------------------------

class VatsimAtcEngine:
    """The tracker logic. A mixin: combine it with a Host, engine first (see Host above)."""

    # ---- Lifecycle -------------------------------------------------------

    def initialize(self) -> None:
        self._load_config()
        self._init_state()
        self._last_aircraft_attrs = self._load_last_aircraft()

        # Every timer is scheduled before any loader thread is started, and a thread that
        # cannot be started is logged, not raised. A failed start used to abort this method
        # partway, leaving an entry that had set up successfully but never polled.
        # Each periodic refresh first fires one full interval after start. The loads at the
        # end of this method are the initial ones; firing the refresh seconds later would
        # repeat them.
        self.run_every(
            self._schedule_geometry_refresh,
            self.datetime() + timedelta(seconds=self.geometry_refresh_interval_seconds),
            self.geometry_refresh_interval_seconds,
        )
        self.run_every(
            self._retry_missing_loads,
            self.datetime() + timedelta(seconds=STARTUP_RETRY_FIRST_SECONDS),
            STARTUP_RETRY_INTERVAL_SECONDS,
        )

        if self.navdata_path:
            self.run_every(
                self._schedule_navdata_refresh,
                self.datetime() + timedelta(seconds=self.navdata_refresh_interval_seconds),
                self.navdata_refresh_interval_seconds,
            )
            self.run_every(
                self._check_navdata_changed,
                self.datetime() + timedelta(seconds=NAVDATA_CHANGE_CHECK_SECONDS),
                NAVDATA_CHANGE_CHECK_SECONDS,
            )
        else:
            self.log("No navdata_path configured; route-line resolution disabled "
                     "(sensor.vatsim_flight_route will report navdata_unavailable)",
                     level="INFO")

        # NAT tracks are only useful alongside local navdata, since a
        # track's named entry/exit points resolve against fix_coords.
        if self.navdata_path and self.nat_tracks_url:
            self.run_every(
                self._schedule_nat_track_refresh,
                self.datetime() + timedelta(seconds=self.nat_track_refresh_interval_seconds),
                self.nat_track_refresh_interval_seconds,
            )

        # A typed-in airport used to wait for the next poll (up to ~20 s), so a value
        # typed and cleared inside one poll was never seen. React to the helper directly.
        self.listen_state(self._on_atis_override_change, ATIS_AIRPORT_OVERRIDE_ENTITY_ID)

        self.run_in(self._poll_cycle, 1)

        # The initial loads. _retry_missing_loads covers boundaries and navdata if a start
        # fails here; the NAT refresh timer covers the tracks.
        self._start_geometry_worker("vatsim-geom-loader")
        if self.navdata_path:
            self._start_navdata_worker("vatsim-navdata-loader")
            if self.nat_tracks_url:
                self._start_loader_thread(self._load_nat_tracks_worker, "vatsim-nat-loader")

    def _start_loader_thread(self, target, name: str) -> Optional[threading.Thread]:
        """Start a daemon loader thread. Returns None, with a warning, if it cannot be started
        (thread limits, memory): the caller's timers try again later."""
        try:
            thread = threading.Thread(target=target, daemon=True, name=name)
            thread.start()
            return thread
        except Exception as exc:
            self.log(f"Could not start {name}: {exc}; it will be tried again", level="WARNING")
            return None

    def terminate(self) -> None:
        self._persist_last_aircraft(force=True)

    def set_state(self, entity_id, **kwargs):
        # Remember what HA should hold so _reconcile_published_states can
        # put it back after an HA restart or a failed write without recomputing anything.
        # Recorded before the call: a write that raises is then repaired on the next poll.
        state = kwargs.get("state")
        attrs = dict(kwargs.get("attributes") or {})
        attrs.pop(PUBLISH_TOKEN_ATTR, None)
        prev_state, prev_attrs = self._published_states.get(entity_id, (None, {}))
        merged = dict(prev_attrs)
        merged.update(attrs)
        token = self._published_tokens.get(entity_id)
        if token is None or state != prev_state or merged != prev_attrs:
            self._publish_seq += 1
            token = f"{self._publish_boot}-{self._publish_seq}"
        self._published_tokens[entity_id] = token
        self._published_states[entity_id] = (state, merged)
        attrs[PUBLISH_TOKEN_ATTR] = token
        kwargs["attributes"] = attrs
        return super().set_state(entity_id, **kwargs)

    # ---- Config ------------------------------------------------------

    _CID_PLACEHOLDER_VALUES = {"", "replace_me", "your_cid", "your_vatsim_cid", "0"}

    def _load_config(self) -> None:
        raw_cid = self.args.get("vatsim_cid")
        if raw_cid is None or str(raw_cid).strip().lower() in self._CID_PLACEHOLDER_VALUES:
            raise ValueError(
                "vatsim_cid is not configured (missing or still the example placeholder "
                "value). Set it to your VATSIM Certificate ID, which is a number."
            )
        self.vatsim_cid = str(raw_cid).strip()
        self.poll_interval = float(self.args.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS))
        self.lookahead_minutes = float(self.args.get("lookahead_minutes", DEFAULT_LOOKAHEAD_MINUTES))
        self.min_groundspeed_kt = float(self.args.get("minimum_groundspeed_knots", DEFAULT_MIN_GROUNDSPEED_KT))
        self.altitude_hysteresis_ft = float(self.args.get("altitude_hysteresis_feet", DEFAULT_ALTITUDE_HYSTERESIS_FT))
        self.presence_absent_after_misses = int(
            self.args.get("presence_absent_after_misses", DEFAULT_PRESENCE_ABSENT_AFTER_MISSES)
        )
        self.stale_feed_multiplier = float(self.args.get("stale_feed_multiplier", DEFAULT_STALE_FEED_MULTIPLIER))
        self.stale_feed_grace_seconds = float(
            self.args.get("stale_feed_grace_seconds", DEFAULT_STALE_FEED_GRACE_SECONDS)
        )
        self.app_dep_altitude_gate_ft = float(
            self.args.get("app_dep_altitude_gate_ft", DEFAULT_APP_DEP_ALTITUDE_GATE_FT)
        )
        self.vatspy_boundaries_url = self.args.get("vatspy_boundaries_url", DEFAULT_VATSPY_BOUNDARIES_URL)
        self.vatspy_dat_url = self.args.get("vatspy_dat_url", DEFAULT_VATSPY_DAT_URL)
        self.simaware_boundaries_path = self.args.get("simaware_boundaries_path", DEFAULT_SIMAWARE_BOUNDARIES_PATH)
        geometry_refresh_interval_days = float(
            self.args.get("geometry_refresh_interval_days", DEFAULT_GEOMETRY_REFRESH_INTERVAL_DAYS)
        )
        self.geometry_refresh_interval_seconds = geometry_refresh_interval_days * 24 * 3600
        self.navdata_path = self.args.get("navdata_path", DEFAULT_NAVDATA_PATH)
        navdata_refresh_interval_days = float(
            self.args.get("navdata_refresh_interval_days", DEFAULT_NAVDATA_REFRESH_INTERVAL_DAYS)
        )
        self.navdata_refresh_interval_seconds = navdata_refresh_interval_days * 24 * 3600
        # Set nat_tracks_url to null or empty to disable track
        # expansion entirely; route lines then skip NATx tokens as before.
        self.nat_tracks_url = self.args.get("nat_tracks_url", NAT_TRACKS_URL)
        self.nat_track_refresh_interval_seconds = float(
            self.args.get("nat_track_refresh_interval_minutes",
                          DEFAULT_NAT_TRACK_REFRESH_INTERVAL_MINUTES)
        ) * 60
        self.track_history_max_points = int(
            self.args.get("track_history_max_points", DEFAULT_TRACK_HISTORY_MAX_POINTS)
        )
        self.track_history_min_distance_nm = float(
            self.args.get("track_history_min_distance_nm", DEFAULT_TRACK_HISTORY_MIN_DISTANCE_NM)
        )
        self.track_history_min_seconds = float(
            self.args.get("track_history_min_seconds", DEFAULT_TRACK_HISTORY_MIN_SECONDS)
        )
        self.destination_range_nm = float(
            self.args.get("destination_range_nm", DEFAULT_DESTINATION_RANGE_NM)
        )
        # metar_fallback: false turns the METAR fetches off entirely.
        self.metar_fallback = str(self.args.get("metar_fallback", True)).strip().lower() not in (
            "false", "0", "no", "off", "none", "")
        self.metar_refresh_seconds = max(
            60.0, float(self.args.get("metar_refresh_minutes", DEFAULT_METAR_REFRESH_MINUTES)) * 60)

    def _init_state(self) -> None:
        self._geometry: Optional[GeometrySnapshot] = None
        self._geometry_version_counter = 0
        self._resolved_ctr_cache: Dict[Tuple[int, str, str], Optional["CtrFeature"]] = {}
        self._last_geometry_refresh_error: Optional[str] = None
        self._last_geometry_refresh_error_ts: Optional[float] = None
        # time.monotonic() of the last health check that ran to its end. Read from another
        # thread by the host's problem reporter; a plain float, so the read is safe.
        self.last_health_check: Optional[float] = None
        # Set when a saved aircraft position exists but could not be used. Cleared by the
        # next successful save. No file at all is a new install, not an error.
        self._last_aircraft_restore_error: Optional[str] = None

        self._navdata: Optional[NavDataSnapshot] = None
        self._navdata_version_counter = 0
        self._navdata_loading = False
        self._navdata_start_lock = threading.Lock()
        self._last_navdata_refresh_error: Optional[str] = None
        self._last_navdata_refresh_error_ts: Optional[float] = None
        self._nat: Optional[NatTrackSnapshot] = None
        self._nat_version_counter = 0
        self._last_nat_refresh_error: Optional[str] = None
        self._last_nat_refresh_error_ts: Optional[float] = None
        self._last_route_key: Optional[Tuple[str, str, str]] = None
        self._last_route_published: Optional[Tuple] = None
        self._destination_in_range = False
        self._last_destination_published: Optional[Tuple] = None
        self._airborne_latched = False
        self._latched_departure: Optional[str] = None
        self._fast_polls = 0
        self._stopped_polls = 0
        self._ground_altitude_ft: Optional[float] = None
        self._last_atis_list: List[Any] = []
        # Touched only on the host loop; fetch threads hand results back via run_in.
        self._metar_cache: Dict[str, Dict[str, Any]] = {}
        # (point, departure, arrival) from the last connected poll, for republishing.
        self._destination_ctx: Optional[Tuple[Any, str, str]] = None
        self._metar_inflight: Dict[str, float] = {}
        self._metar_last_attempt: Dict[str, float] = {}
        self._metar_wanted_ts: Optional[float] = None
        self._metar_status: Optional[str] = None
        self._metar_source_last: Optional[str] = None
        self._last_metar_error: Optional[str] = None
        self._last_metar_error_ts: Optional[float] = None

        self._poll_in_flight = False
        self._consecutive_feed_failures = 0
        self._backoff_seconds = self.poll_interval
        self._last_feed_success_ts: Optional[float] = None
        self._last_feed_health_published: Optional[Tuple] = None
        self._runtime_unresolved_controller_count = 0

        self._presence_state = PresenceState.UNKNOWN
        self._presence_miss_streak = 0
        self._last_seen_ts: Optional[float] = None
        self._last_presence_published: Optional[Tuple] = None

        self._current_debounce = DebounceTracker()
        self._last_current_published: Optional[Tuple] = None
        self._last_current_identity: Optional[SectorIdentity] = None
        self._current_altitude_above: Optional[bool] = None

        self._next_identity_debounce = DebounceTracker()
        self._last_next_published: Optional[Tuple] = None
        self._next_altitude_above: Optional[bool] = None

        self._altitude_history: List[Tuple[float, float]] = []  # (ts, altitude_ft)
        self._heading_history: List[Tuple[float, float]] = []   # (ts, heading_deg)
        self._session_track_points: List[Dict[str, Any]] = []
        self._session_track_segment = 0
        self._session_track_break = False
        self._session_track_started_at: Optional[float] = None
        self._last_tracker_published: Optional[Tuple] = None
        self._last_session_track_published: Optional[Tuple] = None
        self._last_online_boundaries_published: Optional[Tuple[int, int]] = None

        # entity_id -> (state, attributes as published). Merged on every set_state: a publish
        # adds to the attributes already there, so a host shows the merged set.
        self._published_states: Dict[str, Tuple[Any, Dict[str, Any]]] = {}
        self._processing_error_active = False
        self._last_poll_error_sig: Optional[str] = None
        self._last_poll_error_ts = 0.0
        self._last_aircraft_attrs: Optional[Dict[str, Any]] = None
        self._last_aircraft_persisted_ts = 0.0
        self._last_restore_ts: Optional[float] = None
        # Per-entity publication token and unconfirmed republish count.
        self._published_tokens: Dict[str, str] = {}
        # Random, not the clock: an instance recreated in the same second
        # restarted the sequence, repeated the token and hid a rejected first write.
        self._publish_boot = uuid.uuid4().hex[:12]
        self._publish_seq = 0
        self._publish_attempts: Dict[str, int] = {}
        self._publish_failing_notified = False
        self._publish_verify_unreachable = False

    # ---- Geometry loading (off main thread, atomic swap) ------------------

    def _schedule_geometry_refresh(self, kwargs) -> None:
        self._start_geometry_worker("vatsim-geom-refresh")

    def _start_geometry_worker(self, name: str) -> None:
        # One load at a time: a retry or refresh that fires during a slow load is skipped.
        thread = getattr(self, "_geometry_thread", None)
        if thread is not None and thread.is_alive():
            return
        self._geometry_thread = self._start_loader_thread(self._load_geometry_worker, name)

    def _retry_missing_loads(self, kwargs) -> None:
        """Try again whatever is still not loaded. A start with no network yet, or one failed
        download, would otherwise leave boundaries missing until the weekly refresh. Once
        everything is loaded this does nothing. NAT tracks refresh often enough on their own."""
        if self._geometry is None:
            self.log("Boundaries not loaded; retrying the load", level="INFO")
            self._start_geometry_worker("vatsim-geom-retry")
        if self.navdata_path and self._navdata is None:
            self._start_navdata_worker("vatsim-navdata-retry")  # skips itself while a load is running

    def _load_geometry_worker(self) -> None:
        try:
            self._simaware_load_issue_count = 0
            ctr_raw = self._fetch_and_parse_vatspy()
            tracon_features = self._fetch_and_parse_simaware()
        except Exception as exc:
            if getattr(exc, "kind", "") == "Stopped":
                return  # the host is shutting down mid-load: not a failure worth reporting
            self._last_geometry_refresh_error = f"{getattr(exc, 'kind', type(exc).__name__)}: {exc}"
            self._last_geometry_refresh_error_ts = time.time()
            self.log(f"Geometry load failed ({exc}); continuing to use the previous "
                     f"geometry snapshot (version={self._geometry.version if self._geometry else None}). "
                     f"A missing snapshot is retried within minutes, an existing one at the next "
                     f"scheduled refresh", level="WARNING")
            return

        self._geometry_version_counter += 1
        snapshot = GeometrySnapshot(
            version=self._geometry_version_counter,
            ctr_raw=ctr_raw,
            tracon_features=tuple(tracon_features),
            loaded_at=time.time(),
            simaware_load_issue_count=self._simaware_load_issue_count,
        )
        self.run_in(self._apply_geometry_snapshot, 0, snapshot=snapshot)

    def _apply_geometry_snapshot(self, kwargs) -> None:
        snapshot: GeometrySnapshot = kwargs["snapshot"]
        self._geometry = snapshot
        self._last_geometry_refresh_error = None
        self._last_geometry_refresh_error_ts = None
        self._resolved_ctr_cache = {}

        if self.simaware_boundaries_path and len(snapshot.tracon_features) == 0:
            self.log(
                "simaware_boundaries_path is configured but yielded zero usable TRACON "
                "features; APP/DEP coverage will not function until this is fixed "
                "(check the path and file schema) - see app_dep_status on "
                "sensor.vatsim_feed_health", level="WARNING",
            )

        self.log(
            f"Geometry snapshot swapped in (version={snapshot.version}, "
            f"vatspy_fir_prefixes={len(snapshot.ctr_raw.prefix_to_boundary_id)}, "
            f"vatspy_boundary_features={len(snapshot.ctr_raw.boundary_features_by_id)}, "
            f"usable_boundaries={'/'.join(str(n) for n in getattr(self, '_usable_boundary_counts', ('?', '?')))}, "
            f"tracon_features={len(snapshot.tracon_features)}, "
            f"app_dep_status={self._compute_app_dep_status()}, "
            f"simaware_load_issues={snapshot.simaware_load_issue_count})"
        )

    def _fetch_and_parse_vatspy(self) -> VatspyRawData:
        boundaries_geojson = json.loads(self.http_get(self.vatspy_boundaries_url, timeout=15))

        prefix_to_boundary_id = self._parse_vatspy_dat_firs(self.http_get(self.vatspy_dat_url, timeout=15))

        boundary_features_by_id: Dict[str, Any] = {}
        for feature in boundaries_geojson.get("features", []):
            fid = (feature.get("properties", {}) or {}).get("id") or feature.get("id")
            if fid:
                boundary_features_by_id[str(fid)] = feature

        # An empty or mismatched download would otherwise become a "ready"
        # snapshot that resolves every position as uncontrolled, which the
        # geometry_status watchdog cannot see. Raising makes the worker keep
        # the previous snapshot (or stay "loading" at startup) and record the
        # error on sensor.vatsim_feed_health.
        if not prefix_to_boundary_id or not boundary_features_by_id:
            raise ValueError(
                f"VATSpy returned no usable data (fir_prefixes={len(prefix_to_boundary_id)}, "
                f"boundaries={len(boundary_features_by_id)})"
            )
        if not any(bid in boundary_features_by_id
                   for ids in prefix_to_boundary_id.values() for bid in ids):
            raise ValueError("VATSpy FIR prefixes match none of the boundary ids")
        # Ids can all line up while the shapes are missing. That would also be a "ready"
        # snapshot in which nobody is ever covered, so it is refused the same way.
        # Counted against every boundary the FIR list refers to, present or not, so a file
        # that is merely short is caught as well as one whose shapes are empty.
        referenced = {bid for ids in prefix_to_boundary_id.values() for bid in ids}
        usable = sum(1 for bid in referenced
                     if bid in boundary_features_by_id and self._has_outline(boundary_features_by_id[bid]))
        if usable < MIN_USABLE_BOUNDARY_SHARE * len(referenced):
            raise ValueError(
                f"VATSpy boundaries are usable for only {usable} of the {len(referenced)} "
                f"boundaries the FIR list refers to"
            )
        self._usable_boundary_counts = (usable, len(referenced))

        return VatspyRawData(
            prefix_to_boundary_id=prefix_to_boundary_id,
            boundary_features_by_id=boundary_features_by_id,
        )

    @staticmethod
    def _has_outline(feature: Any) -> bool:
        """True if the feature's geometry builds into a boundary. Uses the same parser as the
        coverage lookups, so "accepted here" and "usable there" cannot disagree."""
        geometry = feature.get("geometry") if isinstance(feature, dict) else None
        try:
            return geo.boundary_from_geojson(geometry) is not None
        except Exception:
            return False

    @staticmethod
    def _cap_feed_text(text: Any) -> Any:
        """Cut free text from the feed so that it stays under MAX_FEED_TEXT_BYTES once written
        as JSON. Measured in serialized bytes, not characters: control characters and
        non-ASCII text take several bytes each, and the recorder's limit is in bytes."""
        if not isinstance(text, str):
            return text
        def size(candidate: str) -> int:
            return len(json.dumps(candidate, ensure_ascii=False).encode("utf-8"))

        # A lone surrogate (valid in JSON, not in UTF-8) cannot be written to a state at all;
        # it becomes "?" here so that one odd character cannot stop a publish.
        text = text[:MAX_FEED_TEXT_BYTES].encode("utf-8", "replace").decode("utf-8")
        if size(text) <= MAX_FEED_TEXT_BYTES:
            return text
        low, high = 0, len(text)                     # longest prefix that fits
        while low < high:
            middle = (low + high + 1) // 2
            if size(text[:middle]) <= MAX_FEED_TEXT_BYTES:
                low = middle
            else:
                high = middle - 1
        return text[:low]

    def _parse_vatspy_dat_firs(self, dat_text: str) -> Dict[str, Tuple[str, ...]]:
        prefix_to_boundary_ids: Dict[str, List[str]] = {}
        seen_pairs = set()
        in_firs_section = False
        for line in dat_text.splitlines():
            line = line.strip()
            if not line or line.startswith(";"):
                continue
            if line.startswith("["):
                in_firs_section = line.upper() == "[FIRS]"
                continue
            if not in_firs_section:
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            icao = parts[0].strip()
            prefix = parts[2].strip()
            boundary_id = parts[3].strip() if len(parts) > 3 and parts[3].strip() else icao
            if not prefix or not boundary_id:
                continue

            pair = (prefix, boundary_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            existing = prefix_to_boundary_ids.setdefault(prefix, [])
            if existing:
                # A normal property of the published data, handled at poll time. Not a warning:
                # it would otherwise be logged on every load, for every user.
                self.log(f"VATSpy prefix '{prefix}' maps to multiple distinct boundary "
                         f"ids ({', '.join(existing)}, '{boundary_id}'); keeping all as "
                         f"candidates for covers()-based resolution at poll time",
                         level="INFO")
            existing.append(boundary_id)

        return {prefix: tuple(ids) for prefix, ids in prefix_to_boundary_ids.items()}

    def _fetch_and_parse_simaware(self) -> List[TraconFeature]:
        if not self.simaware_boundaries_path:
            self.log("No simaware_boundaries_path configured; running in "
                     "CTR/FSS-only mode (APP/DEP coverage disabled)", level="INFO")
            return []

        path = self.simaware_boundaries_path
        if not os.path.exists(path):
            self.log(f"simaware_boundaries_path '{path}' does not exist; "
                     f"running in CTR/FSS-only mode this cycle (fail-soft)", level="WARNING")
            return []

        if os.path.isdir(path):
            file_paths = sorted(glob.glob(os.path.join(path, "*.geojson")))
            if not file_paths:
                self.log(f"No *.geojson files found under '{path}'; "
                         f"running in CTR/FSS-only mode", level="WARNING")
        else:
            file_paths = [path]

        tracon_features: List[TraconFeature] = []
        for file_path in file_paths:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    document = json.load(f)
            except Exception as exc:
                self._simaware_load_issue_count += 1
                self.log(f"Failed to read/parse SimAware file '{file_path}': {exc}; "
                         f"skipping (fail-soft)", level="WARNING")
                continue

            if document.get("type") == "FeatureCollection":
                features = document.get("features", [])
            elif document.get("type") == "Feature":
                features = [document]
            else:
                self._simaware_load_issue_count += 1
                self.log(f"SimAware file '{file_path}' is neither a Feature nor a "
                         f"FeatureCollection; skipping (fail-soft)", level="WARNING")
                continue

            for feature in features:
                parsed = self._parse_tracon_feature(feature, len(tracon_features))
                if parsed is not None:
                    tracon_features.append(parsed)

        return tracon_features

    def _parse_tracon_feature(self, feature: dict, index_for_fallback_id: int) -> Optional[TraconFeature]:
        props = feature.get("properties", {}) or {}
        prefixes = props.get("prefix") or []
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        suffix = str(props.get("suffix") or "APP").upper()
        polygon_id = str(props.get("id") or props.get("name") or f"tracon_{index_for_fallback_id}")

        if not prefixes:
            self._simaware_load_issue_count += 1
            self.log(f"SimAware feature '{polygon_id}' has no prefixes; skipping (fail-soft)",
                     level="WARNING")
            return None
        try:
            geom = _boundary_or_raise(feature["geometry"])
            return TraconFeature(
                polygon_id=polygon_id, prefixes=tuple(prefixes), suffix=suffix,
                geometry=geom,
            )
        except Exception as exc:
            self._simaware_load_issue_count += 1
            self.log(f"Failed to parse TRACON geometry '{polygon_id}': {exc}; "
                     f"skipping (fail-soft)", level="WARNING")
            return None

    # ---- Navdata loading (off main thread, atomic swap) --------------------

    def _schedule_navdata_refresh(self, kwargs) -> None:
        self._start_navdata_worker("vatsim-navdata-refresh")

    def _start_navdata_worker(self, name: str) -> None:
        # One load at a time (the hourly change check can fire during a load).
        # The lock closes the check-then-set gap, and a failed start clears
        # the flag or every later refresh would return early for good. The hourly change check
        # retries; navdata_status and vatsim_feed_unhealthy_v2 report a load that never happened.
        with self._navdata_start_lock:
            if self._navdata_loading:
                return
            self._navdata_loading = True
        try:
            threading.Thread(target=self._load_navdata_worker, daemon=True, name=name).start()
        except Exception as exc:
            self._navdata_loading = False
            self.log(f"Could not start the navdata load: {exc}", level="WARNING")

    def _navdata_source_mtime(self) -> float:
        newest = 0.0
        for name in NAVDATA_SOURCE_FILES:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(self.navdata_path, name)))
            except OSError:
                continue
        return newest

    def _check_navdata_changed(self, kwargs) -> None:
        # AIRAC cycles are 28 days and the scheduled refresh is 30, so a new
        # cycle written into navdata_path could sit unused for days. Reload as
        # soon as any source file is newer than the loaded snapshot.
        if not self.navdata_path:
            return
        if self._navdata is None:
            # Nothing loaded yet. The scheduled refresh is 30 days away, so
            # retry here. The start lock skips this if a load is running; navdata_status and
            # vatsim_feed_unhealthy_v2 keep reporting until one succeeds.
            self.log("No navdata loaded; retrying the load", level="INFO")
            self._start_navdata_worker("vatsim-navdata-retry")
            return
        if self._navdata_source_mtime() > self._navdata.source_mtime + 1.0:
            self.log("Navdata files changed on disk; reloading", level="INFO")
            self._schedule_navdata_refresh({})

    @staticmethod
    def _read_navdata_cycle(path: str) -> Tuple[Optional[str], Optional[str]]:
        """(cycle, valid_until as YYYY-MM-DD) from Navigraph's cycle.json
        (validityPeriod "YYYY-MM-DD/YYYY-MM-DD"), else cycle_info.txt
        ("AIRAC cycle : 2611", "Valid (from/to): 29/OCT/2026 - 25/NOV/2026").
        (None, None) when neither is present or parseable; fail-soft."""
        try:
            with open(os.path.join(path, "cycle.json"), "r", encoding="utf-8") as f:
                doc = json.load(f)
            cycle = str(doc.get("cycle") or "").strip() or None
            period = str(doc.get("validityPeriod") or "")
            until = period.split("/")[1].strip() if "/" in period else None
            if until and re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
                return cycle, until
            if cycle:
                return cycle, None
        except (OSError, ValueError, AttributeError):
            pass
        try:
            with open(os.path.join(path, "cycle_info.txt"), "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            m = re.search(r"AIRAC cycle\s*:\s*(\d{4})", text)
            v = re.search(r"Valid \(from/to\):\s*\d{2}/[A-Z]{3}/\d{4}\s*-\s*(\d{2})/([A-Z]{3})/(\d{4})", text)
            until = None
            if v and v.group(2) in _MONTHS:
                until = f"{v.group(3)}-{_MONTHS[v.group(2)]:02d}-{v.group(1)}"
            return (m.group(1) if m else None), until
        except OSError:
            return None, None

    def _load_navdata_worker(self) -> None:
        """Runs entirely on a background thread, mirroring
        _load_geometry_worker's pattern exactly. Never touches
        self._navdata directly except via the single assignment performed
        in _apply_navdata_snapshot, which runs back on the host loop."""
        try:
            self._load_navdata_worker_body()
        finally:
            self._navdata_loading = False

    def _load_navdata_worker_body(self) -> None:
        # Read before parsing: a file written mid-parse leaves the newer mtime
        # unrecorded, so the next change check reloads it.
        source_mtime = self._navdata_source_mtime()
        cycle, valid_until = self._read_navdata_cycle(self.navdata_path)
        try:
            fix_path = os.path.join(self.navdata_path, "earth_fix.dat")
            nav_path = os.path.join(self.navdata_path, "earth_nav.dat")
            awy_path = os.path.join(self.navdata_path, "earth_awy.dat")
            aptmeta_path = os.path.join(self.navdata_path, "earth_aptmeta.dat")

            issue_count = 0
            fix_coords: Dict[str, List[Tuple[float, float]]] = {}

            # Column layouts differ between the two files and are NOT both
            # lat/lon-first:
            #   earth_fix.dat  lat(0) lon(1) ident(2) ...
            #   earth_nav.dat  row_code(0) lat(1) lon(2) elev(3) freq(4)
            #                  range(5) bearing(6) ident(7) ...
            # Passing lat_column=0 for earth_nav.dat parses the row type
            # code (2/3/12/13) as a latitude and the real latitude as a
            # longitude. Because those codes are valid floats the per-line
            # float guard does not reject them, so every navaid loads with
            # silently corrupted coordinates.
            for path, ident_column, lat_column in ((fix_path, 2, 0), (nav_path, 7, 1)):
                if not os.path.exists(path):
                    issue_count += 1
                    self.log(f"Navdata file '{path}' does not exist; identifiers from "
                             f"this file will be unavailable for route resolution "
                             f"(fail-soft)", level="WARNING")
                    continue
                loaded = self._parse_navdata_coord_file(path, ident_column, lat_column)
                for ident, coord in loaded:
                    fix_coords.setdefault(ident, []).append(coord)

            airport_coords: Dict[str, List[Tuple[float, float]]] = {}
            if not os.path.exists(aptmeta_path):
                issue_count += 1
                self.log(f"Navdata file '{aptmeta_path}' does not exist; departure/"
                         f"arrival airport anchoring will be unavailable for route "
                         f"resolution (fail-soft)", level="WARNING")
            else:
                # earth_aptmeta.dat has a DIFFERENT layout (ident-first:
                # ident, region, lat, lon, ...) than earth_fix.dat/
                # earth_nav.dat (lat/lon-first) - uses its own dedicated
                # parser, NOT _parse_navdata_coord_file.
                for ident, coord in self._parse_navdata_airport_file(aptmeta_path):
                    airport_coords.setdefault(ident, []).append(coord)

            airway_graph: Dict[str, Dict[str, List[str]]] = {}
            if not os.path.exists(awy_path):
                issue_count += 1
                self.log(f"Navdata file '{awy_path}' does not exist; airway segments "
                         f"cannot be resolved, only direct fix-to-fix legs will work "
                         f"(fail-soft)", level="WARNING")
            else:
                airway_graph = self._parse_navdata_airways(awy_path)

        except Exception as exc:
            self._last_navdata_refresh_error = f"{type(exc).__name__}: {exc}"
            self._last_navdata_refresh_error_ts = time.time()
            self.log(f"Navdata load failed ({exc}); continuing to use the previous "
                     f"navdata snapshot (version={self._navdata.version if self._navdata else None}) "
                     f"until the next scheduled refresh", level="WARNING")
            return

        self._navdata_version_counter += 1
        snapshot = NavDataSnapshot(
            version=self._navdata_version_counter,
            fix_coords={k: tuple(v) for k, v in fix_coords.items()},
            airport_coords={k: tuple(v) for k, v in airport_coords.items()},
            airway_graph={name: {k: tuple(v) for k, v in adj.items()} for name, adj in airway_graph.items()},
            loaded_at=time.time(),
            load_issue_count=issue_count,
            cycle=cycle,
            valid_until=valid_until,
            source_mtime=source_mtime,
        )
        self.run_in(self._apply_navdata_snapshot, 0, snapshot=snapshot)

    def _schedule_nat_track_refresh(self, kwargs) -> None:
        self._start_loader_thread(self._load_nat_tracks_worker, "vatsim-nat-refresh")

    @staticmethod
    def _parse_nat_oceanic_point(token: str) -> Optional[Tuple[float, float]]:
        """Converts natTrak's oceanic shorthand to (lat, lon). NAT tracks
        are entirely in the northern hemisphere and west of Greenwich, so
        "56/20" is unambiguously 56N 020W. Returns None for anything that
        is not shorthand (named fixes fall through to normal lookup)."""
        match = NAT_OCEANIC_POINT_RE.match(token)
        if not match:
            return None
        lat = float(match.group(1))
        lon = -float(match.group(2))
        if not (0.0 <= lat <= 90.0 and -180.0 <= lon <= 0.0):
            return None
        return (lat, lon)

    @staticmethod
    def _parse_nattrak_timestamp(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    def _load_nat_tracks_worker(self) -> None:
        """Fetches the currently in-force NAT tracks and builds a
        NatTrackSnapshot. Mirrors _load_navdata_worker's threading
        pattern: never touches self._nat directly, hands the snapshot
        back via run_in for a single atomic swap on the host loop.

        Fails soft as a whole: on any error the previous snapshot is
        retained and the error is surfaced on sensor.vatsim_feed_health
        rather than silently degrading route lines."""
        try:
            payload = json.loads(self.http_get(self.nat_tracks_url, timeout=NAT_TRACK_FETCH_TIMEOUT_SECONDS))
            if not isinstance(payload, list):
                raise ValueError(f"expected a JSON array, got {type(payload).__name__}")

            now = datetime.now(timezone.utc)
            fix_coords: Dict[str, List[Tuple[float, float]]] = {}
            airway_graph: Dict[str, Dict[str, List[str]]] = {}
            track_ids: List[str] = []
            earliest_valid_to: Optional[float] = None

            for entry in payload:
                if not isinstance(entry, dict):
                    continue
                identifier = str(entry.get("identifier") or "").strip().upper()
                if not identifier or not entry.get("active"):
                    continue
                # `active` is NOT the same as in force: natTrak marks the
                # next window's tracks active hours before they open, so
                # the validity window is the real gate.
                valid_from = self._parse_nattrak_timestamp(entry.get("valid_from"))
                valid_to = self._parse_nattrak_timestamp(entry.get("valid_to"))
                if valid_from is None or valid_to is None:
                    continue
                if not (valid_from <= now <= valid_to):
                    continue

                tokens = [t for t in str(entry.get("last_routeing") or "").split() if t]
                if len(tokens) < 2:
                    continue

                for token in tokens:
                    coord = self._parse_nat_oceanic_point(token)
                    if coord is not None:
                        fix_coords.setdefault(token, [])
                        if coord not in fix_coords[token]:
                            fix_coords[token].append(coord)

                # Only chain through points we can actually place. A named
                # endpoint that is missing from local navdata would
                # otherwise create a graph node with no coordinate, and
                # _bfs_airway_path would return a path we cannot draw.
                placeable = [
                    t for t in tokens
                    if t in fix_coords
                    or (self._navdata is not None and t in self._navdata.fix_coords)
                ]
                if len(placeable) < 2:
                    continue

                graph: Dict[str, List[str]] = {}
                for first, second in zip(placeable, placeable[1:]):
                    graph.setdefault(first, []).append(second)
                    graph.setdefault(second, []).append(first)

                airway_graph["NAT" + identifier] = graph
                track_ids.append(identifier)
                valid_to_ts = valid_to.timestamp()
                if earliest_valid_to is None or valid_to_ts < earliest_valid_to:
                    earliest_valid_to = valid_to_ts

        except Exception as exc:
            self._last_nat_refresh_error = f"{getattr(exc, 'kind', type(exc).__name__)}: {exc}"
            self._last_nat_refresh_error_ts = time.time()
            self.log(f"NAT track refresh failed ({exc}); retaining previous snapshot "
                     f"(version={self._nat.version if self._nat else None})",
                     level="WARNING")
            return

        self._nat_version_counter += 1
        snapshot = NatTrackSnapshot(
            version=self._nat_version_counter,
            fix_coords={k: tuple(v) for k, v in fix_coords.items()},
            airway_graph={n: {k: tuple(v) for k, v in adj.items()}
                          for n, adj in airway_graph.items()},
            track_ids=tuple(sorted(track_ids)),
            fetched_at=time.time(),
            valid_until=earliest_valid_to,
        )
        self.run_in(self._apply_nat_track_snapshot, 0, snapshot=snapshot)

    def _apply_nat_track_snapshot(self, kwargs) -> None:
        snapshot: NatTrackSnapshot = kwargs["snapshot"]
        self._nat = snapshot
        self._last_nat_refresh_error = None
        self._last_nat_refresh_error_ts = None
        self.log(f"NAT tracks swapped in (version={snapshot.version}, "
                 f"tracks={len(snapshot.track_ids)} [{','.join(snapshot.track_ids)}], "
                 f"oceanic_points={len(snapshot.fix_coords)})", level="INFO")
        # Same invalidation _apply_navdata_snapshot performs, and for the
        # same reason: _maybe_publish_route short-circuits on an unchanged
        # route_key, so without this a route resolved before the tracks
        # loaded keeps its unexpanded line until the filed plan changes.
        # Tracks usually land after navdata on start (network fetch vs local
        # parse); when they do not, _apply_navdata_snapshot triggers a rebuild.
        self._last_route_key = None

    def _compute_metar_status(self, now: float) -> str:
        """disabled, idle (no METAR wanted recently), pending (wanted, no result yet), ok,
        not_found (neither source has the station), error (both sources failed)."""
        if not self.metar_fallback:
            return "disabled"
        if self._metar_wanted_ts is None or now - self._metar_wanted_ts > 2 * self.metar_refresh_seconds:
            return "idle"
        return self._metar_status or "pending"

    def _compute_nat_track_status(self) -> str:
        """Mirrors _compute_navdata_status. 'expired' is called out
        separately from 'empty' because a stale snapshot draws a
        plausible but wrong line, which is worse than drawing none."""
        if not self.nat_tracks_url or not self.navdata_path:
            return "disabled"
        if self._nat is None:
            return "unloaded"
        if not self._nat.track_ids:
            return "no_tracks_in_force"
        if self._nat.valid_until is not None and time.time() > self._nat.valid_until:
            return "expired"
        return "active"

    def _apply_navdata_snapshot(self, kwargs) -> None:
        snapshot: NavDataSnapshot = kwargs["snapshot"]
        self._navdata = snapshot
        self._last_navdata_refresh_error = None
        self._last_navdata_refresh_error_ts = None
        self.log(
            f"Navdata snapshot swapped in (version={snapshot.version}, "
            f"unique_idents={len(snapshot.fix_coords)}, "
            f"unique_airports={len(snapshot.airport_coords)}, "
            f"airways={len(snapshot.airway_graph)}, "
            f"load_issues={snapshot.load_issue_count})"
        )
        self._last_route_key = None
        # The NAT graph only links named entry/exit fixes found in navdata at
        # the moment it was built. Both loaders start together, and on a slow
        # host the network fetch can finish first, leaving every track without
        # its endpoints until the next scheduled NAT refresh. Rebuild now.
        # Cost: one extra natTrak fetch per navdata load (startup, then every
        # navdata_refresh_interval_days).
        if self.nat_tracks_url:
            self._schedule_nat_track_refresh({})

    @staticmethod
    def _parse_navdata_coord_file(
        path: str, ident_column: int, lat_column: int = 0
    ) -> List[Tuple[str, Tuple[float, float]]]:
        """Parses a whitespace-delimited navdata file where `lat_column`
        and `lat_column + 1` hold lat/lon and `ident_column` holds the
        identifier - shared by earth_fix.dat (ident_column=2,
        lat_column=0) and earth_nav.dat (ident_column=7, lat_column=1,
        because that file leads with a row type code).
        NOT used for earth_aptmeta.dat - see _parse_navdata_airport_file,
        since that file's columns are ordered differently. Fails soft per
        line: any line that doesn't have enough fields, or whose lat/lon
        fields aren't parseable floats, is silently skipped - this
        naturally filters out each file's header lines without
        special-casing them by content."""
        results: List[Tuple[str, Tuple[float, float]]] = []
        min_fields = max(ident_column, lat_column + 1)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                parts = raw_line.split()
                if len(parts) <= min_fields:
                    continue
                try:
                    lat = float(parts[lat_column])
                    lon = float(parts[lat_column + 1])
                except ValueError:
                    continue
                # Guard against a future column-layout change silently
                # producing plausible-looking garbage again.
                if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                    continue
                ident = parts[ident_column].strip()
                if not ident:
                    continue
                results.append((ident, (lat, lon)))
        return results

    @staticmethod
    def _parse_navdata_airport_file(path: str) -> List[Tuple[str, Tuple[float, float]]]:
        """Parses earth_aptmeta.dat specifically - its column layout is
        ident(0) region(1) lat(2) lon(3) ..., NOT lat/lon-first like
        earth_fix.dat and earth_nav.dat, so it cannot reuse
        _parse_navdata_coord_file (which assumes columns 0/1 are always
        lat/lon - calling it with ident_column=0 against this file would
        try to parse the airport identifier itself as a latitude float,
        which always fails and silently drops every row). Fails soft per
        line: any line with too few fields, or whose lat/lon fields
        aren't parseable floats, is silently skipped - this is how the
        file's header/version/sentinel lines get filtered out without
        special-casing them by content."""
        results: List[Tuple[str, Tuple[float, float]]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                parts = raw_line.split()
                if len(parts) < 4:
                    continue
                ident = parts[0].strip()
                if not ident:
                    continue
                try:
                    lat = float(parts[2])
                    lon = float(parts[3])
                except ValueError:
                    continue
                results.append((ident, (lat, lon)))
        return results

    @staticmethod
    def _parse_navdata_airways(path: str) -> Dict[str, Dict[str, List[str]]]:
        """Parses earth_awy.dat into {airway_name: {ident: [neighbor_ident, ...]}}.

        Expected whitespace-separated columns (11 total):
          from_id from_region from_type to_id to_region to_type
          direction seq_no base_alt top_alt airway_name(s)

        The final column can contain multiple airway names joined with
        '-' when a segment is shared by more than one named airway; each
        name gets its own adjacency entry for that segment. Direction
        (column 6, 'N' vs 'F') is intentionally ignored - see the
        NavDataSnapshot docstring for why an undirected graph is
        sufficient here. Fails soft per line."""
        airway_graph: Dict[str, Dict[str, List[str]]] = {}
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                parts = raw_line.split()
                if len(parts) < 11:
                    continue
                from_id = parts[0].strip()
                to_id = parts[3].strip()
                if not from_id or not to_id:
                    continue
                airway_names = [n for n in parts[-1].strip().split("-") if n]
                if not airway_names:
                    continue
                for name in airway_names:
                    adj = airway_graph.setdefault(name, {})
                    adj.setdefault(from_id, []).append(to_id)
                    adj.setdefault(to_id, []).append(from_id)
        return airway_graph

    # ---- Poll loop (single-flight, jitter, backoff) -----------------------

    def _poll_cycle(self, kwargs=None) -> None:
        if self._poll_in_flight:
            self._schedule_next_poll(self.poll_interval)
            return
        self._poll_in_flight = True
        try:
            self._run_poll_cycle_body()
            # Only a whole poll, scheduling included, ends the episode.
            self._processing_error_active = False
        except Exception as exc:
            # Full traceback for a new error, or the same one after the window;
            # a repeat inside it is one line at DEBUG so a persistent fault does
            # not flood the log.
            sig = f"{type(exc).__name__}: {exc}"
            t = time.time()
            if sig != self._last_poll_error_sig or t - self._last_poll_error_ts >= RESTORE_REPEAT_WARN_SECONDS:
                self._last_poll_error_sig, self._last_poll_error_ts = sig, t
                self.log(f"Unhandled exception in poll cycle:\n{traceback.format_exc()}", level="ERROR")
            else:
                self.log(f"Poll cycle failed again: {sig}", level="DEBUG")
            # The fetch succeeded (last_success_ts is fresh) but processing did
            # not, so feed_stale cannot see this. timestamp_status carries it to
            # the processing_error trigger in vatsim_feed_unhealthy_v2.
            self._processing_error_active = True
            try:
                self._publish_feed_health(
                    FeedHealth.DEGRADED, time.time(), timestamp_status="processing_error"
                )
            except Exception as exc2:
                self.log(f"Could not publish processing-error health: {exc2}", level="DEBUG")
            self._schedule_next_poll(self.poll_interval)
        finally:
            self._poll_in_flight = False

    def _schedule_next_poll(self, delay_seconds: float) -> None:
        jitter = random.uniform(-POLL_JITTER_SECONDS, POLL_JITTER_SECONDS)
        delay = max(1.0, delay_seconds + jitter)
        self.run_in(self._poll_cycle, delay)

    def _run_poll_cycle_body(self) -> None:
        now = time.time()
        self._reconcile_published_states(now)
        payload, error, feed_ts = self._fetch_feed()

        if error is not None:
            self._consecutive_feed_failures += 1
            self._backoff_seconds = (
                min(self._backoff_seconds * 2, MAX_BACKOFF_SECONDS)
                if self._consecutive_feed_failures > 1 else self.poll_interval
            )
            health = (
                FeedHealth.UNAVAILABLE
                if self._consecutive_feed_failures >= MAX_CONSECUTIVE_FEED_FAILURES
                else FeedHealth.DEGRADED
            )
            self._publish_feed_health(health, now, timestamp_status="unknown")
            self.log(f"VATSIM feed fetch failed ({error}); "
                     f"consecutive_failures={self._consecutive_feed_failures}", level="WARNING")
            self._schedule_next_poll(self._backoff_seconds)
            return

        if feed_ts is None:
            self._consecutive_feed_failures += 1
            self._backoff_seconds = (
                min(self._backoff_seconds * 2, MAX_BACKOFF_SECONDS)
                if self._consecutive_feed_failures > 1 else self.poll_interval
            )
            health = (
                FeedHealth.UNAVAILABLE
                if self._consecutive_feed_failures >= MAX_CONSECUTIVE_FEED_FAILURES
                else FeedHealth.DEGRADED
            )
            self._publish_feed_health(
                health, now, feed_age=None, timestamp_status="missing_or_unparseable"
            )
            self.log(
                "VATSIM feed payload has no parseable general.update_timestamp "
                "(missing field or unexpected format); freshness cannot be verified, "
                "so this cycle is treated as degraded/unavailable rather than "
                "healthy - not updating coverage state this cycle", level="WARNING",
            )
            self._schedule_next_poll(self._backoff_seconds)
            return

        feed_age = now - feed_ts
        stale_threshold = (
            DEFAULT_EXPECTED_FEED_REGEN_SECONDS * self.stale_feed_multiplier
            + self.poll_interval
            + self.stale_feed_grace_seconds
        )
        if feed_age > stale_threshold:
            self._consecutive_feed_failures += 1
            self._backoff_seconds = min(self._backoff_seconds * 2, MAX_BACKOFF_SECONDS)
            self._publish_feed_health(
                FeedHealth.DEGRADED, now, feed_age=feed_age, timestamp_status="ok"
            )
            self.log(f"VATSIM feed payload is stale (age={feed_age:.1f}s, "
                     f"threshold={stale_threshold:.1f}s); treating as degraded, not "
                     f"updating coverage state this cycle", level="WARNING")
            self._schedule_next_poll(self._backoff_seconds)
            return

        self._consecutive_feed_failures = 0
        self._backoff_seconds = self.poll_interval
        self._last_feed_success_ts = now
        self._runtime_unresolved_controller_count = 0
        atis_list = payload.get("atis")
        self._last_atis_list = atis_list if isinstance(atis_list, list) else []

        pilot = self._find_own_pilot(payload)
        if pilot is None:
            self._advance_presence_absent(now)
            if self._presence_state == PresenceState.ABSENT:
                self._publish_destination_offline()
            self._publish_feed_health(FeedHealth.HEALTHY, now, feed_age=feed_age, timestamp_status="ok")
            self._schedule_next_poll(self.poll_interval)
            return

        self._advance_presence_present(now)

        point, altitude_ft, groundspeed_kt, heading_deg = self._extract_pilot_state(pilot)
        self._publish_aircraft_tracker(now, point, altitude_ft, groundspeed_kt, heading_deg, pilot)
        self._maybe_record_session_point(now, point, altitude_ft, groundspeed_kt, heading_deg)
        self._altitude_history.append((now, altitude_ft))
        self._heading_history.append((now, heading_deg))
        self._trim_history()
        self._publish_destination(point, groundspeed_kt, pilot, payload)

        # Checked after presence and telemetry, which need no polygons: a
        # failed geometry load must not also hide the pilot, or the
        # coverage-down alert (gated on pilot connected) could never fire.
        if self._geometry is None:
            self.log("Geometry not yet loaded; skipping coverage evaluation this cycle",
                     level="INFO")
            self._publish_feed_health(FeedHealth.HEALTHY, now, feed_age=feed_age, timestamp_status="ok")
            self._schedule_next_poll(self.poll_interval)
            return

        controllers = self._filter_relevant_controllers(payload)
        geometry = self._geometry

        current_identity, current_coverage = self._resolve_current_coverage(
            point, altitude_ft, controllers, geometry
        )
        self._last_current_identity = current_identity
        self._publish_current_coverage(current_identity, current_coverage)

        self._resolve_and_publish_next_sector(
            point, altitude_ft, groundspeed_kt, heading_deg, controllers, geometry, current_identity
        )

        self._publish_all_online_boundaries(controllers, geometry)

        self._maybe_publish_route(pilot)

        self._publish_feed_health(FeedHealth.HEALTHY, now, feed_age=feed_age, timestamp_status="ok")

        self._schedule_next_poll(self.poll_interval)

    # ---- Feed fetch / freshness -------------------------------------------

    def _fetch_feed(self) -> Tuple[Optional[dict], Optional[str], Optional[float]]:
        try:
            payload = json.loads(self.http_get(VATSIM_FEED_URL, timeout=10))
        except FetchError as exc:
            return None, (f"read timeout: {exc}" if exc.is_timeout else f"request error: {exc}"), None
        except ValueError as exc:
            return None, f"invalid JSON: {exc}", None
        return payload, None, self._extract_feed_timestamp(payload)

    @staticmethod
    def _extract_feed_timestamp(payload: dict) -> Optional[float]:
        ts_str = (payload.get("general", {}) or {}).get("update_timestamp")
        if not ts_str:
            return None
        try:
            from datetime import datetime as _dt
            cleaned = ts_str.replace("Z", "+00:00")
            if "." in cleaned:
                head, _, tail = cleaned.partition(".")
                frac, _, tz = tail.partition("+")
                cleaned = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
            return _dt.fromisoformat(cleaned).timestamp()
        except Exception:
            return None

    def _compute_app_dep_status(self) -> str:
        if not self.simaware_boundaries_path:
            return "disabled"
        if self._geometry is None:
            return "configured_but_unloaded"
        if len(self._geometry.tracon_features) == 0:
            return "configured_but_empty"
        return "active"

    def _compute_navdata_status(self) -> str:
        if not self.navdata_path:
            return "disabled"
        if self._navdata is None:
            return "configured_but_unloaded"
        if len(self._navdata.fix_coords) == 0:
            return "configured_but_empty"
        # Past the AIRAC validity end date (UTC). The data is still used for
        # the route line; this only makes the age visible and alertable.
        if self._navdata_days_expired() > 0:
            return "expired"
        return "active"

    def _navdata_days_expired(self) -> int:
        """Whole days since the last day the loaded cycle was valid, by the UTC date. 0 while
        it is valid, or when the data carries no end date."""
        valid_until = self._navdata.valid_until if self._navdata else None
        if not valid_until:
            return 0
        try:
            last_day = datetime.strptime(valid_until, "%Y-%m-%d").date()
        except ValueError:
            return 0
        return max(0, (datetime.now(timezone.utc).date() - last_day).days)

    def _publish_feed_health(
        self, health: FeedHealth, now: float,
        feed_age: Optional[float] = None, timestamp_status: str = "unknown",
    ) -> None:
        # A fault after the body's own "ok" publish alternated ok and error,
        # so the 5 min hold on processing_error never completed.
        if self._processing_error_active and timestamp_status == "ok":
            health, timestamp_status = FeedHealth.DEGRADED, "processing_error"
        geometry_age = (now - self._geometry.loaded_at) if self._geometry else None
        attrs = {
            "friendly_name": "VATSIM Feed Health",
            "last_success_ts": self._last_feed_success_ts,
            "consecutive_failures": self._consecutive_feed_failures,
            "feed_age_seconds": round(feed_age, 1) if feed_age is not None else None,
            "timestamp_status": timestamp_status,
            "simaware_load_issue_count": (
                self._geometry.simaware_load_issue_count if self._geometry else 0
            ),
            "runtime_unresolved_controller_count": self._runtime_unresolved_controller_count,
            "app_dep_status": self._compute_app_dep_status(),
            "geometry_snapshot_version": self._geometry.version if self._geometry else None,
            "geometry_loaded_at": self._geometry.loaded_at if self._geometry else None,
            "geometry_age_seconds": round(geometry_age, 1) if geometry_age is not None else None,
            "last_geometry_refresh_error": self._last_geometry_refresh_error,
            "last_geometry_refresh_error_ts": self._last_geometry_refresh_error_ts,
            "coverage_evaluation_ready": self._geometry is not None,
            "geometry_status": "ready" if self._geometry is not None else "loading",
            "navdata_status": self._compute_navdata_status(),
            "navdata_cycle": self._navdata.cycle if self._navdata else None,
            "navdata_valid_until": self._navdata.valid_until if self._navdata else None,
            "navdata_days_expired": self._navdata_days_expired(),
            "navdata_snapshot_version": self._navdata.version if self._navdata else None,
            "navdata_load_issue_count": self._navdata.load_issue_count if self._navdata else 0,
            # Set when a load or refresh failed and the previous snapshot (if any) was kept:
            # navdata_status alone still reads "active" in that case.
            "last_navdata_refresh_error": self._last_navdata_refresh_error,
            "last_navdata_refresh_error_ts": self._last_navdata_refresh_error_ts,
            # NAT tracks. Surfaced separately from navdata because they go
            # stale on a scale of hours, not AIRAC cycles - an expired
            # snapshot draws a plausible but wrong oceanic line, so this
            # needs to be visible to an automation.
            "nat_track_status": self._compute_nat_track_status(),
            "nat_track_count": len(self._nat.track_ids) if self._nat else 0,
            "nat_tracks_in_force": ",".join(self._nat.track_ids) if self._nat else "",
            "nat_track_snapshot_version": self._nat.version if self._nat else None,
            "nat_track_age_seconds": (
                round(now - self._nat.fetched_at, 1) if self._nat else None
            ),
            "nat_track_valid_until": self._nat.valid_until if self._nat else None,
            "last_nat_refresh_error": self._last_nat_refresh_error,
            "last_nat_refresh_error_ts": self._last_nat_refresh_error_ts,
            # Idle unless a METAR was wanted within two refresh intervals, so one old
            # failure cannot hold 'error' after the flight. Watched by vatsim_feed_unhealthy_v2.
            "metar_status": self._compute_metar_status(now),
            "metar_source_last": self._metar_source_last,
            "last_metar_error": self._last_metar_error,
            "last_metar_error_ts": self._last_metar_error_ts,
            "last_aircraft_restore_error": self._last_aircraft_restore_error,
            # Entities HA has not accepted after repeated republishes. A zero count
            # and an empty string are dropped in publication; readers use | int(0).
            "publish_unverified_count": len(self._publish_failing()),
            "publish_unverified": ",".join(self._publish_failing()),
        }
        snapshot = (health.value, tuple(sorted(attrs.items())))
        if snapshot != self._last_feed_health_published:
            self.set_state("sensor.vatsim_feed_health", state=health.value, attributes=attrs)
            # Remembered only once the write worked. Remembered first, a write that failed
            # would never be tried again while the health stayed the same.
            self._last_feed_health_published = snapshot
        # Only here, after everything above worked: an unchanged health still counts as a
        # check, a health report that raised does not.
        self.last_health_check = time.monotonic()

    # ---- Pilot presence state machine -------------------------------------

    def _find_own_pilot(self, payload: dict) -> Optional[dict]:
        for pilot in payload.get("pilots", []) or []:
            if str(pilot.get("cid")) == self.vatsim_cid:
                return pilot
        return None

    def _extract_pilot_state(self, pilot: dict) -> Tuple[Point, float, float, float]:
        lat = float(pilot.get("latitude"))
        lon = float(pilot.get("longitude"))
        altitude_ft = float(pilot.get("altitude") or 0.0)
        groundspeed_kt = float(pilot.get("groundspeed") or 0.0)
        heading_deg = float(pilot.get("heading") or 0.0)
        return Point(lon, lat), altitude_ft, groundspeed_kt, heading_deg

    def _publish_aircraft_tracker(
        self, now: float, point: Point, altitude_ft: float, groundspeed_kt: float,
        heading_deg: float, pilot: dict,
    ) -> None:
        attrs = {
            "friendly_name": "VATSIM Aircraft",
            "latitude": round(point.y, 6),
            "longitude": round(point.x, 6),
            "source_type": "gps",
            "gps_accuracy": 0,
            "altitude_ft": round(altitude_ft, 0),
            "groundspeed_kt": round(groundspeed_kt, 0),
            "heading_deg": round(heading_deg, 0),
            "callsign": pilot.get("callsign"),
            "cid": self.vatsim_cid,
            "last_seen_ts": now,
            # Explicit True: the absence publish sets False and attributes merge.
            "session_active": True,
        }
        self._last_aircraft_attrs = attrs
        self._persist_last_aircraft()
        snapshot = ("not_home", tuple(sorted(attrs.items())))
        if snapshot == self._last_tracker_published:
            return
        self._last_tracker_published = snapshot
        self.set_state(AIRCRAFT_TRACKER_ENTITY_ID, state="not_home", attributes=attrs)

    def _last_known_aircraft_attrs(self) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {
            "friendly_name": "VATSIM Aircraft",
            "source_type": "gps",
            "cid": self.vatsim_cid,
        }
        last = self._last_aircraft_attrs or {}
        for key in ("latitude", "longitude", "gps_accuracy", "altitude_ft",
                    "groundspeed_kt", "heading_deg", "callsign", "last_seen_ts"):
            if last.get(key) is not None:
                attrs[key] = last[key]
        attrs["session_active"] = False
        return attrs

    def _last_aircraft_path(self) -> str:
        # Default: next to this file. A host should override this with a path that survives
        # an update of the code, as the integration does.
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            LAST_AIRCRAFT_STATE_FILENAME)

    def _persist_last_aircraft(self, force: bool = False) -> None:
        if self._last_aircraft_attrs is None:
            return
        now = time.time()
        if not force and now - self._last_aircraft_persisted_ts < LAST_AIRCRAFT_PERSIST_MIN_SECONDS:
            return
        path = self._last_aircraft_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"saved_at": now, "attributes": self._last_aircraft_attrs}, fh)
            os.replace(tmp, path)  # atomic, so a crash mid-write cannot leave a torn file
            self._last_aircraft_persisted_ts = now
            self._last_aircraft_restore_error = None   # the file is good again
        except OSError as exc:
            self.log(f"Could not persist last aircraft state to {path}: {exc}", level="WARNING")

    def _load_last_aircraft(self) -> Optional[Dict[str, Any]]:
        path = self._last_aircraft_path()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            self.log(f"No saved aircraft position at {path}; the map has no position "
                     f"until the next flight", level="INFO")
            return None
        except (OSError, ValueError) as exc:
            self._last_aircraft_restore_error = f"{type(exc).__name__}: {exc}"
            self.log(f"Could not read saved aircraft position {path}: {exc}", level="WARNING")
            return None
        attrs = data.get("attributes") if isinstance(data, dict) else None
        try:
            lat = float(attrs["latitude"])
            lon = float(attrs["longitude"])
            valid = -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
        except (TypeError, KeyError, ValueError):
            valid = False
        if not valid:
            self._last_aircraft_restore_error = "the saved file holds no valid position"
            self.log(f"Saved aircraft position in {path} is malformed; ignoring it", level="WARNING")
            return None
        saved_cid = attrs.get("cid")
        if saved_cid is not None and str(saved_cid) != self.vatsim_cid:
            self.log(f"Saved aircraft position in {path} belongs to a different CID; ignoring it",
                     level="WARNING")
            return None
        self.log(f"Loaded saved aircraft position {lat:.4f},{lon:.4f} "
                 f"({attrs.get('callsign')}) from {path}", level="INFO")
        return dict(attrs)

    def _publish_failing(self) -> List[str]:
        return sorted(e for e, n in self._publish_attempts.items() if n >= PUBLISH_FAILING_AFTER_ATTEMPTS)

    def _reconcile_published_states(self, now: float) -> None:
        """Republish any published entity that HA does not hold as this app last wrote it.

        Replaces the device_tracker sentinel: each entity is checked on
        its own, against HA itself (see PUBLISH_VERIFY_TEMPLATE), so one failed member is
        retried on the next poll even when the others restored. States are republished verbatim
        from _published_states, so the dedupe caches stay accurate and no transition alert is
        re-derived.
        """
        expected = dict(self._published_states)
        if AIRCRAFT_TRACKER_ENTITY_ID not in expected and self._last_aircraft_attrs is not None:
            # Nothing published yet this run, so only the saved file knows the position.
            expected[AIRCRAFT_TRACKER_ENTITY_ID] = ("not_home", self._last_known_aircraft_attrs())
        if not expected:
            return  # nothing published yet, nothing to restore against
        try:
            rendered = self.render_template(PUBLISH_VERIFY_TEMPLATE, variables={"ids": sorted(expected)})
        except Exception as exc:  # the plugin already catches; belt and braces
            rendered = None
            self.log(f"Publish check failed: {exc}", level="WARNING")
        if not isinstance(rendered, list):
            # HA unreachable or the render failed: no evidence either way, so nothing is
            # republished (a write now would fail the same way). Logged once per outage.
            if not self._publish_verify_unreachable:
                self._publish_verify_unreachable = True
                self.log(f"Publish check: no answer from HA ({rendered!r}); skipping until it answers",
                         level="WARNING")
            return
        if self._publish_verify_unreachable:
            self._publish_verify_unreachable = False
            self.log("Publish check: HA answering again", level="INFO")
        reported: Dict[str, Any] = {}
        for item in rendered:
            try:
                reported[str(item[0])] = (item[1], item[2])
            except (TypeError, IndexError):
                continue

        lost = []
        for entity_id in sorted(expected):
            # No token: nothing written yet this run and only the saved position is known; existing is enough.
            want = self._published_tokens.get(entity_id)
            if entity_id not in reported:
                lost.append((entity_id, "missing"))
            elif want is not None and reported[entity_id] != (want, str(expected[entity_id][0])):
                lost.append((entity_id, "not updated"))
            else:
                self._publish_attempts.pop(entity_id, None)

        if lost:
            repeat = (self._last_restore_ts is not None
                      and now - self._last_restore_ts < RESTORE_REPEAT_WARN_SECONDS)
            self._last_restore_ts = now
            self.log("HA does not hold " + ", ".join(f"{e} ({why})" for e, why in lost) + "; republishing",
                     level="WARNING" if repeat else "INFO")
        for entity_id, _why in lost:
            state, attrs = expected[entity_id]
            attrs = dict(attrs)
            # vatsim_atc_online_alert fires on last_change_reason ->
            # became_controlled, including when the entity is created, so a
            # verbatim restore mid-flight would re-announce the same controller.
            if (entity_id == "binary_sensor.vatsim_atc_coverage"
                    and attrs.get("last_change_reason") in ("became_controlled", "controller_changed")):
                attrs["last_change_reason"] = "restored"
            # This runs before the feed is read, so the cached "connected" may no
            # longer be true. vatsim_destination_atis_changed needs "connected", so a repaired letter
            # never announces. The next normal publish writes the real value. A letter change whose
            # write failed is therefore not announced: biased toward silence over a stale letter.
            if entity_id == DESTINATION_ENTITY_ID and attrs.get("session") == "connected":
                attrs["session"] = "repaired"
            n = self._publish_attempts.get(entity_id, 0) + 1
            self._publish_attempts[entity_id] = n
            if n == PUBLISH_FAILING_AFTER_ATTEMPTS or (n > PUBLISH_FAILING_AFTER_ATTEMPTS and n % 50 == 0):
                self.log(f"{entity_id} still not held by HA after {n} republishes", level="WARNING")
            try:
                self.set_state(entity_id, state=state, attributes=attrs)
            except Exception as exc:
                self.log(f"Republish of {entity_id} failed: {exc}", level="WARNING")

        # Watchdog for this repair path: report in HA while any entity stays unconfirmed.
        failing = self._publish_failing()
        if failing and not self._publish_failing_notified:
            self._publish_failing_notified = True
            try:
                self.call_service(
                    "persistent_notification/create",
                    notification_id=PUBLISH_NOTIFICATION_ID,
                    title="VATSIM tracker cannot publish",
                    message=("HA has not accepted these after "
                             f"{PUBLISH_FAILING_AFTER_ATTEMPTS} republishes: {', '.join(failing)}. "
                             "Dashboards using them show stale or missing data. Check the "
                             "log of whatever runs the tracker for a failed state write."),
                )
            except Exception as exc:
                self.log(f"Could not raise the publish notification: {exc}", level="WARNING")
        elif not failing and self._publish_failing_notified:
            self._publish_failing_notified = False
            try:
                self.call_service("persistent_notification/dismiss", notification_id=PUBLISH_NOTIFICATION_ID)
            except Exception as exc:
                self.log(f"Could not dismiss the publish notification: {exc}", level="WARNING")

    def _maybe_record_session_point(
        self, now: float, point: Point, altitude_ft: float,
        groundspeed_kt: float, heading_deg: float,
    ) -> None:
        if self._session_track_started_at is None:
            self._session_track_started_at = now

        last = self._session_track_points[-1] if self._session_track_points else None
        should_append = last is None
        if last is not None:
            seconds_since_last = now - float(last["ts"])
            distance_since_last_nm = geo.distance_nm(
                float(last["lat"]), float(last["lon"]), point.y, point.x,
            )
            should_append = (
                seconds_since_last >= self.track_history_min_seconds
                or distance_since_last_nm >= self.track_history_min_distance_nm
            )
            if (self._session_track_break
                    or seconds_since_last >= TRACK_GAP_SECONDS
                    or (distance_since_last_nm >= TRACK_JUMP_MIN_NM
                        and distance_since_last_nm * 3600.0 / max(seconds_since_last, 1.0) >= TRACK_JUMP_KT)):
                self._session_track_segment += 1
        self._session_track_break = False

        if not should_append:
            return

        self._session_track_points.append({
            "ts": round(now, 1),
            "lat": round(point.y, 6),
            "lon": round(point.x, 6),
            "altitude_ft": round(altitude_ft, 0),
            "groundspeed_kt": round(groundspeed_kt, 0),
            "heading_deg": round(heading_deg, 0),
            "seg": self._session_track_segment,
        })
        self._session_track_points = self._session_track_points[-self.track_history_max_points:]
        self._publish_session_track()

    def _session_track_geojson(self) -> str:
        # The Session Map draws this instead of recorder history. History always starts from
        # the last recorded position, so it joined a new spawn to wherever the last session ended.
        segments: Dict[int, List[List[float]]] = {}
        for p in self._session_track_points:
            segments.setdefault(p["seg"], []).append([p["lon"], p["lat"]])
        lines = [coords for _, coords in sorted(segments.items()) if len(coords) >= 2]
        if not lines:
            return EMPTY_TRACK_GEOJSON
        return json.dumps({"type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {},
            "geometry": {"type": "MultiLineString", "coordinates": lines},
        }]})

    def _publish_session_track(self) -> None:
        latest = self._session_track_points[-1] if self._session_track_points else None
        attrs = {
            "friendly_name": "VATSIM Session Track",
            "session_started_at": self._session_track_started_at,
            "point_count": len(self._session_track_points),
            "latest_latitude": latest["lat"] if latest else None,
            "latest_longitude": latest["lon"] if latest else None,
            "points": self._session_track_points,
            "track_geojson": self._session_track_geojson(),
        }
        state = "active" if self._session_track_points else "empty"
        snapshot = (state, tuple(sorted((k, str(v)) for k, v in attrs.items())))
        if snapshot == self._last_session_track_published:
            return
        self._last_session_track_published = snapshot
        self.set_state(SESSION_TRACK_ENTITY_ID, state=state, attributes=attrs)

    def _trim_history(self) -> None:
        keep = max(HEADING_STABILITY_WINDOW, ALTITUDE_TREND_WINDOW) + 1
        self._altitude_history = self._altitude_history[-keep:]
        self._heading_history = self._heading_history[-keep:]

    def _advance_presence_present(self, now: float) -> None:
        self._presence_miss_streak = 0
        self._last_seen_ts = now
        if self._presence_state == PresenceState.UNKNOWN:
            self.log(f"CID {self.vatsim_cid} seen in the feed for the first time this "
                     f"session; presence -> present", level="INFO")
        elif self._presence_state != PresenceState.PRESENT:
            self.log("Pilot record reappeared in feed; presence -> present", level="INFO")
        self._presence_state = PresenceState.PRESENT
        self._publish_presence()

    def _advance_presence_absent(self, now: float) -> None:
        self._presence_miss_streak += 1
        # A healthy feed without the pilot breaks the line, so a respawn before the
        # disconnect clears cannot join two places. One missed poll in flight shows as a small gap.
        self._session_track_break = True

        if self._presence_state in (PresenceState.UNKNOWN, PresenceState.ABSENT):
            if (
                self._presence_state != PresenceState.ABSENT
                and self._presence_miss_streak >= self.presence_absent_after_misses
            ):
                self.log(f"CID {self.vatsim_cid} not seen in the feed after "
                         f"{self._presence_miss_streak} consecutive polls since startup; "
                         f"presence -> absent", level="INFO")
                # ABSENT only after clearing completes: an exception inside the
                # clear leaves the state unchanged, so the next poll retries the
                # whole clear instead of skipping it forever.
                self._clear_coverage_state()
                self._presence_state = PresenceState.ABSENT
            self._publish_presence()
            return

        if self._presence_miss_streak == 1:
            self._presence_state = PresenceState.SUSPECT_MISSING
            self.log(f"CID {self.vatsim_cid} not found this poll (feed otherwise healthy); "
                     f"presence -> suspect_missing (miss 1); retaining last known coverage",
                     level="INFO")
        elif self._presence_miss_streak >= self.presence_absent_after_misses:
            if self._presence_state != PresenceState.ABSENT:
                self.log(f"CID {self.vatsim_cid} missing for {self._presence_miss_streak} "
                         f"consecutive polls; presence -> absent, clearing coverage state",
                         level="INFO")
            # Clear first, then commit ABSENT (see the UNKNOWN branch above).
            self._clear_coverage_state()
            self._presence_state = PresenceState.ABSENT
        else:
            self.log(f"CID {self.vatsim_cid} still missing (miss {self._presence_miss_streak} "
                     f"of {self.presence_absent_after_misses} before absent); retaining "
                     f"last known coverage state", level="INFO")
        self._publish_presence()

    def _publish_presence(self) -> None:
        attrs = {
            "friendly_name": "VATSIM Pilot Connected",
            "presence_state": self._presence_state.value,
            "last_seen_ts": self._last_seen_ts,
            "cid": self.vatsim_cid,
        }
        is_on = self._presence_state in (PresenceState.PRESENT, PresenceState.SUSPECT_MISSING)
        pub = (is_on, tuple(sorted(attrs.items())))
        if pub == self._last_presence_published:
            return
        self._last_presence_published = pub
        self.set_state(
            "binary_sensor.vatsim_pilot_connected",
            state="on" if is_on else "off",
            attributes=attrs,
        )

    def _clear_coverage_state(self) -> None:
        self._last_current_identity = None
        self._current_altitude_above = None
        self._next_altitude_above = None
        self._current_debounce = DebounceTracker()
        self._next_identity_debounce = DebounceTracker()
        self._altitude_history = []
        self._heading_history = []
        self._session_track_points = []
        self._session_track_segment = 0
        self._session_track_break = False
        self._session_track_started_at = None
        self._last_session_track_published = None
        self._last_tracker_published = None
        self._last_route_key = None
        self._destination_in_range = False
        self._reset_airborne_evidence()
        # No destination publish here. The poll body publishes it once the
        # state is ABSENT and pilot_connected is off, so a letter change at disconnect cannot
        # pass the connected gate on vatsim_destination_atis_changed.
        self._publish_empty_online_boundaries()
        self._publish_current_coverage(None, None, force=True)
        self._publish_next_sector_forced(NextSectorState.MATCHES, NextResultKind.STILL_UNCONTROLLED)
        self._publish_route_state(
            RouteResolutionStatus.NO_FLIGHT_PLAN, None, None, None, 0, 0, False, False, force=True
        )
        # Position carried explicitly, so it does not depend on an attribute merge surviving
        # a restart.
        self.set_state(
            AIRCRAFT_TRACKER_ENTITY_ID,
            state="not_home",
            attributes=self._last_known_aircraft_attrs(),
        )
        self._persist_last_aircraft(force=True)
        self.set_state(
            SESSION_TRACK_ENTITY_ID,
            state="empty",
            attributes={
                "friendly_name": "VATSIM Session Track",
                "session_started_at": None,
                "point_count": 0,
                "latest_latitude": None,
                "latest_longitude": None,
                "points": [],
                "track_geojson": EMPTY_TRACK_GEOJSON,
            },
        )

    # ---- Controller filtering / prefix matching ---------------------------

    @staticmethod
    def _filter_relevant_controllers(payload: dict) -> List[dict]:
        return [
            c for c in (payload.get("controllers", []) or [])
            if c.get("facility") in (FACILITY_FSS, FACILITY_APP_DEP, FACILITY_CTR)
        ]

    @staticmethod
    def _prefix_candidates_for_callsign(base_callsign: str) -> List[str]:
        parts = base_callsign.split("_")
        candidates = ["_".join(parts[:i]) for i in range(len(parts), 0, -1)]
        return sorted(set(candidates), key=len, reverse=True)

    def _resolve_ctr_features_for_controller(
        self, controller: dict, geometry: GeometrySnapshot
    ) -> List[Tuple[int, CtrFeature]]:
        callsign = controller.get("callsign", "")
        base = callsign.rsplit("_", 1)[0] if "_" in callsign else callsign
        for candidate in self._prefix_candidates_for_callsign(base):
            boundary_ids = geometry.ctr_raw.prefix_to_boundary_id.get(candidate)
            if not boundary_ids:
                continue

            results: List[Tuple[int, CtrFeature]] = []
            for boundary_id in boundary_ids:
                cache_key = (geometry.version, candidate, boundary_id)
                if cache_key in self._resolved_ctr_cache:
                    cached = self._resolved_ctr_cache[cache_key]
                    if cached is not None:
                        results.append((len(candidate), cached))
                    continue

                raw_feature = geometry.ctr_raw.boundary_features_by_id.get(boundary_id)
                if raw_feature is None:
                    self._runtime_unresolved_controller_count += 1
                    self.log(f"Online controller prefix '{candidate}' -> boundary id "
                             f"'{boundary_id}' has no matching feature in Boundaries.geojson; "
                             f"treating as unresolved for this candidate (fail-soft)",
                             level="WARNING")
                    self._resolved_ctr_cache[cache_key] = None
                    continue

                try:
                    geom = _boundary_or_raise(raw_feature["geometry"])
                    resolved: Optional[CtrFeature] = CtrFeature(
                        fir_id=boundary_id, prefix=candidate, geometry=geom
                    )
                except Exception as exc:
                    self._runtime_unresolved_controller_count += 1
                    self.log(f"Failed to parse geometry for boundary '{boundary_id}' (prefix "
                             f"'{candidate}'): {exc}; treating as unresolved (fail-soft)",
                             level="WARNING")
                    resolved = None

                self._resolved_ctr_cache[cache_key] = resolved
                if resolved is not None:
                    results.append((len(candidate), resolved))

            return results
        return []

    def _match_tracons_for_controller(
        self, controller: dict, tracon_features: Tuple[TraconFeature, ...]
    ) -> List[Tuple[int, TraconFeature]]:
        callsign = controller.get("callsign", "")
        suffix = "DEP" if callsign.upper().endswith("_DEP") else "APP"
        base = callsign.rsplit("_", 1)[0] if "_" in callsign else callsign
        for candidate in self._prefix_candidates_for_callsign(base):
            matches = [f for f in tracon_features if f.suffix == suffix and candidate in f.prefixes]
            # SimAware marks a boundary DEP only when it applies
            # specifically to DEP; otherwise the plain (APP) boundary covers the
            # prefix's DEP callsigns too. Only 8 of 1,355 SimAware features are
            # DEP-specific, so without this BOS_DEP matched nothing. A DEP-specific
            # boundary still wins when one exists for the same prefix.
            if not matches and suffix == "DEP":
                matches = [f for f in tracon_features if f.suffix == "APP" and candidate in f.prefixes]
            if matches:
                return [(len(candidate), feature) for feature in matches]
        return []

    # ---- CTR/FSS identity resolution + altitude gate (global heuristic) --

    def _resolve_ctr_jurisdiction(
        self, point: Point, controllers: List[dict], geometry: GeometrySnapshot
    ) -> Optional[Tuple[dict, CtrFeature]]:
        candidates: List[Tuple[int, dict, CtrFeature]] = []
        for controller in controllers:
            if controller.get("facility") not in (FACILITY_CTR, FACILITY_FSS):
                continue
            for matched_len, feature in self._resolve_ctr_features_for_controller(controller, geometry):
                try:
                    if feature.geometry.covers(point.x, point.y):
                        candidates.append((matched_len, controller, feature))
                except Exception:
                    continue

        if not candidates:
            return None

        candidates.sort(key=lambda c: (-c[0], c[2].fir_id, c[1].get("callsign", "")))
        _, winning_controller, winning_feature = candidates[0]
        return winning_controller, winning_feature

    def _classify_altitude(self, altitude_ft: float, previous_above: Optional[bool]) -> bool:
        threshold_ft = self.app_dep_altitude_gate_ft
        band = self.altitude_hysteresis_ft
        if previous_above is None:
            return altitude_ft > threshold_ft
        if previous_above:
            return altitude_ft > (threshold_ft - band)
        return altitude_ft > (threshold_ft + band)

    def _vertical_trend_ft_per_sec(self) -> Optional[float]:
        if len(self._altitude_history) < 2:
            return None
        recent = self._altitude_history[-ALTITUDE_TREND_WINDOW:]
        deltas = []
        for (t0, a0), (t1, a1) in zip(recent[:-1], recent[1:]):
            dt = t1 - t0
            if dt > 0:
                deltas.append((a1 - a0) / dt)
        if not deltas:
            return None
        signs = {1 if d > 0 else (-1 if d < 0 else 0) for d in deltas}
        if len(signs - {0}) > 1:
            return None
        return sum(deltas) / len(deltas)

    def _heading_stability_status(self) -> Tuple[bool, bool]:
        if len(self._heading_history) < HEADING_STABILITY_WINDOW:
            return False, True
        recent = [h for _, h in self._heading_history[-HEADING_STABILITY_WINDOW:]]
        for a, b in zip(recent[:-1], recent[1:]):
            if abs(self._heading_delta(a, b)) > HEADING_STABILITY_MAX_DELTA_DEG:
                return False, False
        return True, False

    @staticmethod
    def _heading_delta(a: float, b: float) -> float:
        return (b - a + 180) % 360 - 180

    @staticmethod
    def _geometry_to_geojson_mapping(geom: Any) -> Optional[dict]:
        if geom is None:
            return None
        try:
            return geo.boundary_to_geojson(geom, BOUNDARY_GEOJSON_SIMPLIFY_TOLERANCE_DEG)
        except Exception:
            return None

    @classmethod
    def _geometry_to_geojson_str(cls, geom: Any) -> Optional[str]:
        mapping_geom = cls._geometry_to_geojson_mapping(geom)
        if mapping_geom is None:
            return None
        try:
            return json.dumps(mapping_geom)
        except Exception:
            return None

    def _publish_all_online_boundaries(
        self, controllers: List[dict], geometry: GeometrySnapshot
    ) -> None:
        features: List[dict] = []
        seen_ids: set = set()

        for controller in controllers:
            facility = controller.get("facility")
            callsign = controller.get("callsign")
            frequency = controller.get("frequency")

            if facility in (FACILITY_CTR, FACILITY_FSS):
                for _matched_len, feature in self._resolve_ctr_features_for_controller(controller, geometry):
                    if feature.fir_id in seen_ids:
                        continue
                    mapping_geom = self._geometry_to_geojson_mapping(feature.geometry)
                    if mapping_geom is None:
                        continue
                    seen_ids.add(feature.fir_id)
                    features.append({
                        "type": "Feature",
                        "geometry": mapping_geom,
                        "properties": {
                            "callsign": callsign,
                            "frequency": frequency,
                            "facility": "CTR",
                            "boundary_id": feature.fir_id,
                        },
                    })
            elif facility == FACILITY_APP_DEP:
                for _matched_len, feature in self._match_tracons_for_controller(controller, geometry.tracon_features):
                    if feature.polygon_id in seen_ids:
                        continue
                    mapping_geom = self._geometry_to_geojson_mapping(feature.geometry)
                    if mapping_geom is None:
                        continue
                    seen_ids.add(feature.polygon_id)
                    features.append({
                        "type": "Feature",
                        "geometry": mapping_geom,
                        "properties": {
                            "callsign": callsign,
                            "frequency": frequency,
                            "facility": feature.suffix,
                            "boundary_id": feature.polygon_id,
                        },
                    })

        try:
            geojson_str = json.dumps({"type": "FeatureCollection", "features": features})
        except Exception:
            return

        snapshot = (len(features), hash(geojson_str))
        if snapshot == self._last_online_boundaries_published:
            return
        self._last_online_boundaries_published = snapshot

        self.set_state(
            ONLINE_ATC_BOUNDARIES_ENTITY_ID,
            state=str(len(features)),
            attributes={
                "friendly_name": "VATSIM Online ATC Boundaries",
                "controller_count": len(features),
                "all_boundaries_geojson": geojson_str,
            },
        )

    def _publish_empty_online_boundaries(self) -> None:
        """Offline, no controllers to show. Same shape and dedupe as _publish_all_online_boundaries
        with zero features; the reconciliation then restores it after an HA restart."""
        geojson_str = json.dumps({"type": "FeatureCollection", "features": []})
        snapshot = (0, hash(geojson_str))
        if snapshot == self._last_online_boundaries_published:
            return
        self._last_online_boundaries_published = snapshot
        self.set_state(
            ONLINE_ATC_BOUNDARIES_ENTITY_ID,
            state="0",
            attributes={
                "friendly_name": "VATSIM Online ATC Boundaries",
                "controller_count": 0,
                "all_boundaries_geojson": geojson_str,
            },
        )

    # ---- Precedence resolution (current + next share this logic) ---------

    def _resolve_best_tracon(
        self, point: Point, controllers: List[dict], geometry: GeometrySnapshot
    ) -> Optional[Tuple[dict, TraconFeature]]:
        candidates: List[Tuple[int, dict, TraconFeature]] = []
        for controller in controllers:
            if controller.get("facility") != FACILITY_APP_DEP:
                continue
            for matched_len, feature in self._match_tracons_for_controller(controller, geometry.tracon_features):
                try:
                    if feature.geometry.covers(point.x, point.y):
                        candidates.append((matched_len, controller, feature))
                except Exception:
                    continue

        if not candidates:
            return None

        candidates.sort(key=lambda c: (-c[0], c[2].polygon_id, c[1].get("callsign", "")))
        _, winning_controller, winning_feature = candidates[0]
        return winning_controller, winning_feature

    def _apply_precedence(
        self, ctr_candidate: Optional[Tuple[dict, CtrFeature]],
        app_candidate: Optional[Tuple[dict, TraconFeature]], mode: Mode,
    ) -> Tuple[SectorIdentity, Optional[ResolvedCoverage]]:
        if app_candidate is not None:
            controller, feature = app_candidate
            identity = SectorIdentity(mode, controller.get("callsign"), feature.polygon_id)
            return identity, ResolvedCoverage(identity, controller.get("frequency"), feature.suffix, feature.geometry)

        if ctr_candidate is not None:
            controller, feature = ctr_candidate
            identity = SectorIdentity(mode, controller.get("callsign"), feature.fir_id)
            return identity, ResolvedCoverage(identity, controller.get("frequency"), "CTR", feature.geometry)

        return SectorIdentity(mode, None, None), None

    def _resolve_current_coverage(
        self, point: Point, altitude_ft: float, controllers: List[dict], geometry: GeometrySnapshot
    ) -> Tuple[SectorIdentity, Optional[ResolvedCoverage]]:
        ctr_jurisdiction = self._resolve_ctr_jurisdiction(point, controllers, geometry)
        above = self._classify_altitude(altitude_ft, self._current_altitude_above)
        self._current_altitude_above = above
        app_candidate = None if above else self._resolve_best_tracon(point, controllers, geometry)
        return self._apply_precedence(ctr_jurisdiction, app_candidate, Mode.CURRENT)

    # ---- Current coverage publishing (identity-keyed debounce) -----------

    def _publish_current_coverage(
        self, identity: Optional[SectorIdentity], coverage: Optional[ResolvedCoverage], force: bool = False
    ) -> None:
        raw_key = identity.as_tuple() if identity else (Mode.CURRENT.value, None, None)
        should_publish = force or self._current_debounce.offer(raw_key)
        if not should_publish:
            return

        is_on = coverage is not None
        previous_is_on = self._last_current_published[0] if self._last_current_published else None

        if previous_is_on is None:
            reason = "became_controlled" if is_on else "initial_uncontrolled"
        elif previous_is_on and is_on:
            reason = "controller_changed"
        elif previous_is_on and not is_on:
            reason = "became_uncontrolled" if not force else "cleared_on_absence"
        elif not previous_is_on and is_on:
            reason = "became_controlled"
        else:
            reason = "cleared_on_absence" if force else "still_uncontrolled"

        attrs = {
            "friendly_name": "VATSIM ATC Coverage",
            "controller_callsign": coverage.identity.controller_callsign if coverage else None,
            "frequency": coverage.frequency if coverage else None,
            "polygon_id": coverage.identity.polygon_id if coverage else None,
            "mode": coverage.facility if coverage else None,
            "last_change_reason": reason,
            "boundary_geojson": self._geometry_to_geojson_str(coverage.geometry) if coverage else None,
        }
        snapshot = (is_on, tuple(sorted(attrs.items())))
        if snapshot == self._last_current_published and not force:
            return
        self._last_current_published = snapshot
        self.log(f"Publishing current coverage: on={is_on} reason={reason} "
                 f"controller={attrs['controller_callsign']} polygon={attrs['polygon_id']}",
                 level="INFO")
        self.set_state(
            "binary_sensor.vatsim_atc_coverage",
            state="on" if is_on else "off",
            attributes=attrs,
        )

    # ---- Next-sector resolution and publishing ----------------------------

    def _project_point(self, point: Point, groundspeed_kt: float, heading_deg: float) -> Point:
        distance_nm = groundspeed_kt * (self.lookahead_minutes / 60.0)
        lat, lon = geo.destination(point.y, point.x, heading_deg, distance_nm)
        return Point(lon, lat)

    def _estimate_next_sector_eta(
        self, current_point: Point, projected_point: Point, groundspeed_kt: float,
        next_coverage: Optional[ResolvedCoverage],
    ) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        if next_coverage is None or next_coverage.geometry is None or groundspeed_kt <= 0:
            return None, None, None

        geometry_obj = next_coverage.geometry
        try:
            if geometry_obj.covers(current_point.x, current_point.y):
                return 0.0, 0.0, "now"
            # The first point where the projected track meets the sector outline, nearest to us.
            entry = geo.first_crossing(geometry_obj, (current_point.x, current_point.y),
                                       (projected_point.x, projected_point.y))
        except Exception:
            return None, None, None
        if entry is None:
            return None, None, None

        distance_nm = geo.distance_nm(current_point.y, current_point.x, entry[1], entry[0])
        eta_minutes = (distance_nm / groundspeed_kt) * 60.0
        if eta_minutes < 0:
            return None, None, None

        if eta_minutes < 1.0:
            eta_text = "<1 min"
        else:
            eta_text = f"{round(eta_minutes):.0f} min"
        return eta_minutes, distance_nm, eta_text

    def _resolve_and_publish_next_sector(
        self, point: Point, altitude_ft: float, groundspeed_kt: float, heading_deg: float,
        controllers: List[dict], geometry: GeometrySnapshot, current_identity: Optional[SectorIdentity],
    ) -> None:
        if groundspeed_kt < self.min_groundspeed_kt:
            self.log(f"Groundspeed {groundspeed_kt:.0f}kt below minimum "
                     f"{self.min_groundspeed_kt:.0f}kt; skipping lookahead this cycle",
                     level="INFO")
            self._publish_next_sector_forced(NextSectorState.SKIPPED_LOW_SPEED, NextResultKind.SKIPPED_LOW_SPEED)
            return

        heading_stable, insufficient_history = self._heading_stability_status()
        if not heading_stable:
            if insufficient_history:
                self.log("Insufficient heading history yet; skipping lookahead "
                         "this cycle (not an instability signal)", level="INFO")
                self._publish_next_sector_forced(
                    NextSectorState.SKIPPED_INSUFFICIENT_HISTORY,
                    NextResultKind.SKIPPED_INSUFFICIENT_HISTORY,
                )
            else:
                self.log("Heading unstable; skipping lookahead this cycle",
                         level="INFO")
                self._publish_next_sector_forced(
                    NextSectorState.SKIPPED_UNSTABLE_HEADING,
                    NextResultKind.SKIPPED_UNSTABLE_HEADING,
                )
            return

        projected_point = self._project_point(point, groundspeed_kt, heading_deg)

        vertical_trend = self._vertical_trend_ft_per_sec()
        app_excluded = False
        app_reason: Optional[str] = None

        if vertical_trend is None:
            app_excluded = True
            app_reason = "ambiguous_trend"
            projected_altitude_ft = altitude_ft
        else:
            projected_altitude_ft = altitude_ft + vertical_trend * (self.lookahead_minutes * 60.0)

        projected_ctr = self._resolve_ctr_jurisdiction(projected_point, controllers, geometry)

        if not app_excluded:
            above = self._classify_altitude(projected_altitude_ft, self._next_altitude_above)
            self._next_altitude_above = above
            if above:
                app_excluded = True
                app_reason = "altitude_gate"

        app_candidate = None if app_excluded else self._resolve_best_tracon(projected_point, controllers, geometry)

        next_identity, next_coverage = self._apply_precedence(projected_ctr, app_candidate, Mode.NEXT)
        eta_minutes, eta_distance_nm, eta_text = self._estimate_next_sector_eta(
            point, projected_point, groundspeed_kt, next_coverage
        )
        self._publish_next_sector_identity(
            next_identity, next_coverage, current_identity, app_excluded, app_reason,
            eta_minutes, eta_distance_nm, eta_text,
        )

    def _publish_next_sector_identity(
        self, next_identity: SectorIdentity, next_coverage: Optional[ResolvedCoverage],
        current_identity: Optional[SectorIdentity], app_excluded: bool, app_reason: Optional[str],
        eta_minutes: Optional[float], eta_distance_nm: Optional[float], eta_text: Optional[str],
    ) -> None:
        raw_key = next_identity.as_tuple()
        # offer() only returns True on a CHANGE of identity, so gating on it
        # froze ETA/note for a stable identity. Gate on stability instead; the
        # snapshot compare below suppresses unchanged republishes.
        self._next_identity_debounce.offer(raw_key)
        if self._next_identity_debounce.consecutive < DEBOUNCE_THRESHOLD:
            return

        current_key = current_identity.as_tuple()[1:] if current_identity else (None, None)
        next_key = raw_key[1:]
        has_current = current_identity is not None and current_key != (None, None)
        has_next = next_coverage is not None

        if has_next and has_current:
            differs = next_key != current_key
            kind = NextResultKind.DIFFERENT_SECTOR if differs else NextResultKind.SAME_SECTOR
        elif has_next and not has_current:
            differs = True
            kind = NextResultKind.DIFFERENT_SECTOR
        elif not has_next and has_current:
            differs = True
            kind = NextResultKind.PROJECTED_UNCONTROLLED
        else:
            differs = False
            kind = NextResultKind.STILL_UNCONTROLLED

        state = NextSectorState.DIFFERS if differs else NextSectorState.MATCHES
        controller_callsign = next_coverage.identity.controller_callsign if next_coverage else None
        frequency = next_coverage.frequency if next_coverage else None
        polygon_id = next_coverage.identity.polygon_id if next_coverage else None
        projection_note = app_reason if app_excluded else None
        attrs = {
            "friendly_name": "VATSIM Next Sector",
            "next_controller_callsign": controller_callsign,
            "next_frequency": frequency,
            "next_polygon_id": polygon_id,
            "app_dep_excluded_from_projection": app_excluded,
            "app_dep_exclusion_reason": app_reason,
            "next_result_kind": kind.value,
            "eta_minutes": round(eta_minutes, 1) if eta_minutes is not None else None,
            "eta_distance_nm": round(eta_distance_nm, 1) if eta_distance_nm is not None else None,
            "eta_text": eta_text,
            "controller_callsign": controller_callsign,
            "frequency": frequency,
            "polygon_id": polygon_id,
            "result_kind": kind.value,
            "projection_note": projection_note,
            "next_boundary_geojson": self._geometry_to_geojson_str(next_coverage.geometry) if next_coverage else None,
        }
        snapshot = (state.value, tuple(sorted(attrs.items())))
        if snapshot == self._last_next_published:
            return
        self._last_next_published = snapshot
        self.log(f"Publishing next-sector result: state={state.value} kind={kind.value} "
                 f"controller={attrs['next_controller_callsign']}", level="INFO")
        self.set_state("sensor.vatsim_next_sector", state=state.value, attributes=attrs)

    def _publish_next_sector_forced(self, state: NextSectorState, kind: NextResultKind) -> None:
        # A forced skip state replaces whatever identity was published, so the
        # debounce must forget it; otherwise the same identity coming back after
        # the skip never republishes and the sensor stays stuck in skipped_*.
        self._next_identity_debounce = DebounceTracker()
        attrs = {
            "friendly_name": "VATSIM Next Sector",
            "next_controller_callsign": None,
            "next_frequency": None,
            "next_polygon_id": None,
            "app_dep_excluded_from_projection": None,
            "app_dep_exclusion_reason": None,
            "next_result_kind": kind.value,
            "eta_minutes": None,
            "eta_distance_nm": None,
            "eta_text": None,
            "controller_callsign": None,
            "frequency": None,
            "polygon_id": None,
            "result_kind": kind.value,
            "projection_note": kind.value,
            "next_boundary_geojson": None,
        }
        snapshot = (state.value, tuple(sorted(attrs.items())))
        if snapshot == self._last_next_published:
            return
        self._last_next_published = snapshot
        self.set_state("sensor.vatsim_next_sector", state=state.value, attributes=attrs)

    # ---- Route resolution (filed flight plan -> map line) -----------------

    @staticmethod
    def _nearest_coord_for_ident(
        ident_map: Dict[str, Tuple[Tuple[float, float], ...]], ident: str,
        reference: Optional[Tuple[float, float]],
    ) -> Optional[Tuple[float, float]]:
        """Resolves a bare identifier to ONE (lat, lon) coordinate, using
        whichever of the given ident_map's candidates (fix_coords or
        airport_coords) is closest to `reference` when more than one
        exists. Identifiers are not globally unique (see NavDataSnapshot's
        docstring), so this is a simple, effective disambiguation
        heuristic for a display-only route line, not a claim of
        navigational correctness. Returns None if the identifier has no
        candidates at all."""
        candidates = ident_map.get(ident)
        if not candidates:
            return None
        if len(candidates) == 1 or reference is None:
            return candidates[0]
        ref_lat, ref_lon = reference
        return min(candidates, key=lambda c: (c[0] - ref_lat) ** 2 + (c[1] - ref_lon) ** 2)

    def _bfs_airway_path(
        self, airway_name: str, start_ident: str, end_ident: str, navdata: NavDataSnapshot
    ) -> Optional[List[str]]:
        """Breadth-first search over the (undirected, see NavDataSnapshot
        docstring) adjacency graph for one named airway, from start_ident
        to end_ident inclusive. Returns None if the airway is unknown,
        either endpoint isn't part of it, or no path exists within
        MAX_AIRWAY_BFS_VISITED_NODES."""
        graph = navdata.airway_graph.get(airway_name)
        if not graph or start_ident not in graph or end_ident not in graph:
            return None
        if start_ident == end_ident:
            return [start_ident]

        visited = {start_ident}
        queue: deque = deque([[start_ident]])
        nodes_visited = 0

        while queue:
            path = queue.popleft()
            current = path[-1]
            for neighbor in graph.get(current, ()):
                if neighbor == end_ident:
                    return path + [neighbor]
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                nodes_visited += 1
                if nodes_visited > MAX_AIRWAY_BFS_VISITED_NODES:
                    return None
                queue.append(path + [neighbor])
        return None

    def _resolve_route_to_points(
        self, route_str: str, navdata: NavDataSnapshot
    ) -> Tuple[List[Tuple[float, float]], int, int]:
        """Parses a free-text ICAO route string into an ordered list of
        (lat, lon) points. Returns (points, resolved_waypoint_count,
        unresolved_token_count). Deliberately does NOT touch airport
        anchoring - that's handled separately in _maybe_publish_route so
        it can apply even when the en-route token parsing fails entirely.

        Recognized token types:
          - a bare waypoint/navaid identifier -> resolved via
            _nearest_coord_for_ident against fix_coords, disambiguated
            against the last resolved point.
          - an airway name immediately followed by another resolvable
            identifier -> the segment between the previous resolved
            waypoint and this new one is expanded via _bfs_airway_path.
          - 'DCT' -> skipped, not counted as unresolved.
          - anything else that fails to resolve as either a plain
            identifier or a known airway name -> skipped, counted as
            unresolved.
        """
        # Overlay the in-force NAT tracks so a filed NATx token resolves
        # through the SAME path as a named airway (fix, airway, fix) with
        # no special-casing below. ChainMap rather than a merged copy:
        # navdata.fix_coords holds ~218k entries and copying it on every
        # route change would be pure waste. Local navdata is searched
        # first, so it wins on any key collision.
        nat = self._nat
        if nat is not None and self._compute_nat_track_status() == "active":
            navdata = replace(
                navdata,
                fix_coords=ChainMap(navdata.fix_coords, nat.fix_coords),
                airway_graph=ChainMap(navdata.airway_graph, nat.airway_graph),
            )

        tokens = [t.strip() for t in route_str.split() if t.strip()]
        points: List[Tuple[float, float]] = []
        resolved_count = 0
        unresolved_count = 0
        last_ident: Optional[str] = None
        last_point: Optional[Tuple[float, float]] = None
        pending_airway: Optional[str] = None

        for token in tokens:
            token_upper = token.upper()
            # ICAO field 15 attaches a speed/level change to a waypoint
            # with a slash: NEEKO/N0495F430, DULBA/M083F350. Strip it so
            # the bare identifier resolves. Do NOT strip unconditionally:
            # oceanic lat/lon shorthand waypoints are also slashed
            # (59/20, 60/30) and those are real identifiers, so only
            # strip when the remainder parses as a speed/level group.
            if "/" in token_upper:
                head, _, tail = token_upper.partition("/")
                if head and ICAO_SPEED_LEVEL_RE.match(tail):
                    token_upper = head
            if not token_upper or token_upper in ROUTE_TOKEN_SKIP_WORDS:
                continue

            if token_upper in navdata.airway_graph:
                pending_airway = token_upper
                continue

            coord = self._nearest_coord_for_ident(navdata.fix_coords, token_upper, last_point)
            if coord is None:
                unresolved_count += 1
                pending_airway = None
                continue

            if pending_airway is not None and last_ident is not None:
                path_idents = self._bfs_airway_path(pending_airway, last_ident, token_upper, navdata)
                if path_idents:
                    running_ref = last_point
                    # [1:-1] not [1:]: _bfs_airway_path returns the path
                    # with BOTH endpoints included, and the end point is
                    # appended again immediately below as `coord`. Using
                    # [1:] duplicates every airway segment's exit fix.
                    for mid_ident in path_idents[1:-1]:
                        mid_coord = self._nearest_coord_for_ident(navdata.fix_coords, mid_ident, running_ref)
                        if mid_coord is not None:
                            points.append(mid_coord)
                            running_ref = mid_coord
                pending_airway = None

            points.append(coord)
            resolved_count += 1
            last_ident = token_upper
            last_point = coord

        return points, resolved_count, unresolved_count

    def _maybe_publish_route(self, pilot: dict) -> None:
        """Checks the pilot's OWN filed flight plan (sourced entirely from
        the VATSIM feed - see module docstring) and republishes the route
        line only when the route/departure/arrival actually changed."""
        flight_plan = pilot.get("flight_plan") or {}
        route_str = (flight_plan.get("route") or "").strip()
        departure = flight_plan.get("departure")
        arrival = flight_plan.get("arrival")
        route_key = (route_str, departure or "", arrival or "")

        if route_key == self._last_route_key:
            return
        self._last_route_key = route_key

        if not route_str:
            self._publish_route_state(
                RouteResolutionStatus.NO_FLIGHT_PLAN, departure, arrival, None, 0, 0, False, False
            )
            return

        navdata = self._navdata
        if navdata is None:
            self._publish_route_state(
                RouteResolutionStatus.NAVDATA_UNAVAILABLE, departure, arrival, route_str, 0, 0, False, False
            )
            return

        points, resolved_count, unresolved_count = self._resolve_route_to_points(route_str, navdata)

        departure_coord = (
            self._nearest_coord_for_ident(navdata.airport_coords, departure.upper(), points[0] if points else None)
            if departure else None
        )
        arrival_coord = (
            self._nearest_coord_for_ident(navdata.airport_coords, arrival.upper(), points[-1] if points else None)
            if arrival else None
        )
        if departure_coord is not None:
            points = [departure_coord] + points
        if arrival_coord is not None:
            points = points + [arrival_coord]

        if not points:
            self._publish_route_state(
                RouteResolutionStatus.UNRESOLVED, departure, arrival, route_str,
                resolved_count, unresolved_count, departure_coord is not None, arrival_coord is not None,
            )
            return

        try:
            geojson_str = (
                json.dumps(geo.linestring_geojson(points)) if len(points) > 1
                else json.dumps({"type": "MultiPoint", "coordinates": [[lon, lat] for lat, lon in points]})
            )
        except Exception:
            self._publish_route_state(
                RouteResolutionStatus.UNRESOLVED, departure, arrival, route_str,
                resolved_count, unresolved_count, departure_coord is not None, arrival_coord is not None,
            )
            return

        self._publish_route_state(
            RouteResolutionStatus.OK, departure, arrival, route_str,
            resolved_count, unresolved_count, departure_coord is not None, arrival_coord is not None,
            geojson_str=geojson_str,
        )

    # ---- Airport ATIS: departure, arrival, or typed in --------------

    @staticmethod
    def _select_destination_atis(payload: dict, icao: str, role: str = "arrival") -> Optional[dict]:
        """ATIS station for icao from the feed's atis list. Split-ATIS airports publish
        <ICAO>_A_ATIS (arrival) and <ICAO>_D_ATIS (departure); a combined one is <ICAO>_ATIS.
        arrival: A, then combined, then any other except D. departure: D, then combined, then
        any other except A. selected: combined, then A, then D, then any other."""
        prefix = icao.upper() + "_"
        stations = [a for a in (payload.get("atis", []) or [])
                    if isinstance(a, dict) and str(a.get("callsign", "")).upper().startswith(prefix)]
        by_cs = {str(a.get("callsign", "")).upper(): a for a in stations}
        order = {"arrival": ("A_ATIS", "ATIS"), "departure": ("D_ATIS", "ATIS"),
                 "selected": ("ATIS", "A_ATIS", "D_ATIS")}.get(role, ("A_ATIS", "ATIS"))
        for suffix in order:
            if prefix + suffix in by_cs:
                return by_cs[prefix + suffix]
        excluded = {"arrival": "_D_ATIS", "departure": "_A_ATIS"}.get(role)
        others = [a for cs, a in sorted(by_cs.items()) if not (excluded and cs.endswith(excluded))]
        return others[0] if others else None

    def _atis_override(self) -> Optional[str]:
        """The typed-in airport, upper case, or None for automatic (blank, missing helper, error)."""
        try:
            value = self.get_state(ATIS_AIRPORT_OVERRIDE_ENTITY_ID)
        except Exception:
            return None
        value = str(value or "").strip().upper()
        if not value or value in ("UNKNOWN", "UNAVAILABLE", "NONE"):
            return None
        return value

    def _atis_attrs(self, payload: dict, icao: str, role: str) -> Dict[str, Any]:
        atis = self._select_destination_atis(payload, icao, role)
        text = (atis or {}).get("text_atis") or []
        attrs = {
            "airport": icao, "role": role, "range_nm": self.destination_range_nm,
            "atis_online": atis is not None,
            "atis_callsign": (atis or {}).get("callsign"),
            "atis_code": ((atis or {}).get("atis_code") or None),
            "atis_frequency": (atis or {}).get("frequency"),
            "atis_text": self._cap_feed_text(" ".join(str(x) for x in text) if isinstance(text, list)
                                             else str(text)),
            "atis_last_updated": (atis or {}).get("last_updated"),
        }
        if atis is not None:
            attrs["weather_source"] = "atis"
        else:
            attrs.update(self._metar_attrs(icao))
        return attrs

    # ---- METAR fallback ---------------------------------------------

    def _metar_attrs(self, icao: str) -> Dict[str, Any]:
        """Cached METAR for icao, and a background fetch when the cache is due. Never blocks
        the poll: the first call for an airport returns weather_source none and the result
        appears on a later poll. Strings, not None, where a reader tests the value, because
        some hosts drop None and False."""
        out: Dict[str, Any] = {"weather_source": "none", "metar_text": None, "metar_source": None,
                               "metar_observed_at": None, "flight_category": None}
        icao = (icao or "").strip().upper()
        if not self.metar_fallback or not re.fullmatch(r"[A-Z0-9]{3,4}", icao):
            return out
        now = time.time()
        self._metar_wanted_ts = now
        started = self._metar_inflight.get(icao)
        stuck = started is not None and now - started > METAR_INFLIGHT_STALE_SECONDS
        if (started is None or stuck) and now - self._metar_last_attempt.get(icao, 0.0) >= self.metar_refresh_seconds:
            self._metar_last_attempt[icao] = now
            self._metar_inflight[icao] = now
            if self._start_loader_thread(lambda: self._fetch_metar_worker(icao), f"vatsim-metar-{icao}") is None:
                self._metar_inflight.pop(icao, None)   # not running: let the next refresh try again
        entry = self._metar_cache.get(icao)
        if entry:
            basis = entry.get("observed_ts") or entry.get("fetched_at") or 0.0
            if now - basis <= METAR_MAX_AGE_SECONDS:
                out.update({
                    "weather_source": entry["source"],
                    "metar_text": entry["raw"],
                    "metar_source": entry["source"],
                    "metar_observed_at": (datetime.fromtimestamp(entry["observed_ts"], timezone.utc).isoformat()
                                          if entry.get("observed_ts") else None),
                    "flight_category": entry.get("flight_category"),
                })
        return out

    def _fetch_metar_worker(self, icao: str) -> None:
        """Runs on its own thread. Touches no instance state; the result goes back through
        run_in so the cache and health fields change only on the host loop."""
        result: Dict[str, Any] = {"icao": icao, "fetched_at": time.time(), "source": None, "raw": None,
                                  "observed_ts": None, "flight_category": None, "error": None,
                                  "not_found": False}
        errors: List[str] = []
        try:
            try:
                text = self.http_get(METAR_VATSIM_URL.format(icao=icao), headers=METAR_HEADERS,
                                     timeout=METAR_FETCH_TIMEOUT_SECONDS)
                line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
                # The service answers 200 with an empty body for a station it does not have.
                if line and icao in line.split()[:2]:
                    result.update(source="metar_vatsim", raw=line)
            except (FetchError, ValueError) as exc:
                errors.append(f"vatsim: {getattr(exc, 'kind', type(exc).__name__)}: {exc}")
            if result["raw"] is None:
                try:
                    text = self.http_get(METAR_AWC_URL.format(icao=icao), headers=METAR_HEADERS,
                                         timeout=METAR_FETCH_TIMEOUT_SECONDS)
                    # An empty body (204) means no report for that station.
                    rows = json.loads(text) if text.strip() else []
                    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
                    if row and str(row.get("rawOb") or "").strip():
                        result.update(source="metar_awc", raw=str(row["rawOb"]).strip())
                        if isinstance(row.get("obsTime"), (int, float)):
                            result["observed_ts"] = float(row["obsTime"])
                        cat = str(row.get("fltCat") or "").upper()
                        if cat in FLIGHT_CATEGORIES:
                            result["flight_category"] = cat
                except (FetchError, ValueError) as exc:
                    errors.append(f"awc: {getattr(exc, 'kind', type(exc).__name__)}: {exc}")
            if result["raw"] is not None:
                if result["observed_ts"] is None:
                    result["observed_ts"] = parse_metar_obs_time(result["raw"], datetime.now(timezone.utc))
                if result["flight_category"] is None:
                    result["flight_category"] = metar_flight_category(result["raw"])
            elif errors:
                result["error"] = "; ".join(errors)[:300]
            else:
                result["not_found"] = True
        except Exception as exc:
            result["raw"] = None
            result["error"] = f"unexpected: {type(exc).__name__}: {exc}"[:300]
        finally:
            self.run_in(self._apply_metar_result, 0, result=result)

    def _apply_metar_result(self, kwargs) -> None:
        r: Dict[str, Any] = kwargs["result"]
        icao = r["icao"]
        self._metar_inflight.pop(icao, None)
        if r.get("raw"):
            self._metar_cache[icao] = {k: r[k] for k in ("source", "raw", "observed_ts", "flight_category", "fetched_at")}
            self._metar_status = "ok"
            self._metar_source_last = r["source"]
            self._last_metar_error = None
            self._last_metar_error_ts = None
        elif r.get("not_found"):
            self._metar_cache.pop(icao, None)
            self._metar_status = "not_found"
        else:
            # Keep the last good METAR: _metar_attrs drops it once METAR_MAX_AGE_SECONDS old.
            self._metar_status = "error"
            self._last_metar_error = f"{icao}: {r.get('error')}"
            self._last_metar_error_ts = r["fetched_at"]
            self.log(f"METAR fetch failed for {icao}: {r.get('error')}", level="WARNING")
        self._republish_destination()

    def _airport_distance_nm(self, icao: str, point: Point) -> Optional[float]:
        navdata = self._navdata
        coord = (self._nearest_coord_for_ident(navdata.airport_coords, icao, (point.y, point.x))
                 if navdata is not None else None)
        if coord is None:
            return None
        return geo.distance_nm(point.y, point.x, coord[0], coord[1])

    def _publish_destination_offline(self) -> None:
        """Not connected: a typed-in airport still shows (state selected, from the last good feed),
        otherwise not_connected."""
        try:
            icao = self._atis_override()
            if icao:
                attrs = self._atis_attrs({"atis": self._last_atis_list}, icao, "selected")
                attrs["distance_nm"] = None
                self._publish_destination_state("selected", attrs)
            else:
                self._publish_destination_state("not_connected", {})
        except Exception as exc:
            self.log(f"Airport ATIS update failed: {exc}", level="WARNING")

    def _reset_airborne_evidence(self) -> None:
        self._airborne_latched = False
        self._latched_departure = None
        self._fast_polls = 0
        self._stopped_polls = 0
        self._ground_altitude_ft = None

    def _update_airborne_latch(self, point: Point, groundspeed_kt: float, pilot: dict, departure: str) -> None:
        # Refiled on the ground with a new departure: back to the departure phase. In the air
        # (a diversion changes the arrival) the latch holds.
        if departure != (self._latched_departure or "") and groundspeed_kt <= TAKEOFF_GROUNDSPEED_KT:
            self._reset_airborne_evidence()
        self._latched_departure = departure or None

        if self._airborne_latched:
            near = False
            if groundspeed_kt <= LANDED_GROUNDSPEED_KT and departure:
                d = self._airport_distance_nm(departure, point)
                near = d is not None and d <= DEPARTURE_NEAR_NM
            self._stopped_polls = self._stopped_polls + 1 if near else 0
            if self._stopped_polls >= LANDED_CONFIRM_POLLS:
                self._reset_airborne_evidence()
                self._latched_departure = departure or None
            return

        altitude = pilot.get("altitude")
        altitude_ft = float(altitude) if isinstance(altitude, (int, float)) else None
        if altitude_ft is not None:
            self._ground_altitude_ft = (altitude_ft if self._ground_altitude_ft is None
                                        else min(self._ground_altitude_ft, altitude_ft))
        if groundspeed_kt <= TAKEOFF_GROUNDSPEED_KT:
            self._fast_polls = 0
            return
        self._fast_polls += 1
        if self._fast_polls < TAKEOFF_CONFIRM_POLLS:
            return
        climbed = (altitude_ft is not None
                   and altitude_ft - self._ground_altitude_ft >= TAKEOFF_CLIMB_FT)
        if climbed or not departure:
            self._airborne_latched = True
            return
        d = self._airport_distance_nm(departure, point)
        self._airborne_latched = d is not None and d > DEPARTURE_NEAR_NM

    def _publish_destination(self, point: Point, groundspeed_kt: float, pilot: dict, payload: dict) -> None:
        """sensor.vatsim_destination. State: selected (typed-in airport), departure (on the ground
        within DEPARTURE_NEAR_NM of the filed departure, before takeoff), in_range / out_of_range
        (filed arrival, airborne), at_other_airport (on the ground before takeoff, away from the
        filed departure), no_destination, position_unknown, not_connected. The card shows for
        selected, departure and in_range. Fail-soft: an error logs a WARNING and keeps the
        previous state; it must not stop coverage evaluation."""
        try:
            flight_plan = pilot.get("flight_plan") or {}
            departure = str(flight_plan.get("departure") or "").strip().upper()
            arrival = str(flight_plan.get("arrival") or "").strip().upper()
            self._update_airborne_latch(point, groundspeed_kt, pilot, departure)
            self._destination_ctx = (point, departure, arrival)
            self._compose_destination(point, departure, arrival, payload)
        except Exception as exc:
            self.log(f"Airport ATIS update failed: {exc}", level="WARNING")

    def _compose_destination(self, point: Point, departure: str, arrival: str, payload: dict) -> None:
        """Everything in _publish_destination after the airborne latch. No per-poll counters
        change here, so it is safe to call again between polls."""
        try:
            override = self._atis_override()
            if override:
                attrs = self._atis_attrs(payload, override, "selected")
                d = self._airport_distance_nm(override, point)
                attrs["distance_nm"] = None if d is None else round(d, 1)
                self._publish_destination_state("selected", attrs)
                return

            if not self._airborne_latched:
                if not departure:
                    self._publish_destination_state("no_destination", {})
                    return
                attrs = self._atis_attrs(payload, departure, "departure")
                d = self._airport_distance_nm(departure, point)
                attrs["distance_nm"] = None if d is None else round(d, 1)
                if d is None:
                    self._publish_destination_state("position_unknown", attrs)
                else:
                    self._publish_destination_state("departure" if d <= DEPARTURE_NEAR_NM else "at_other_airport", attrs)
                return

            if not arrival:
                self._destination_in_range = False
                self._publish_destination_state("no_destination", {})
                return
            attrs = self._atis_attrs(payload, arrival, "arrival")
            d = self._airport_distance_nm(arrival, point)
            if d is None:
                self._destination_in_range = False
                attrs["distance_nm"] = None
                self._publish_destination_state("position_unknown", attrs)
                return
            attrs["distance_nm"] = round(d, 1)
            if self._destination_in_range:
                self._destination_in_range = d <= self.destination_range_nm + DESTINATION_RANGE_HYSTERESIS_NM
            else:
                self._destination_in_range = d <= self.destination_range_nm
            self._publish_destination_state("in_range" if self._destination_in_range else "out_of_range", attrs)
        except Exception as exc:
            self.log(f"Airport ATIS update failed: {exc}", level="WARNING")

    def _republish_destination(self) -> None:
        """Recompute sensor.vatsim_destination from the last feed's ATIS list and the last
        position. Connected: the full choice. Otherwise: typed-in airport or not_connected."""
        try:
            if self._presence_state == PresenceState.PRESENT and self._destination_ctx is not None:
                point, departure, arrival = self._destination_ctx
                self._compose_destination(point, departure, arrival, {"atis": self._last_atis_list})
            else:
                self._publish_destination_offline()
        except Exception as exc:
            self.log(f"Airport ATIS republish failed: {exc}", level="WARNING")

    def _on_atis_override_change(self, entity, attribute, old, new, *args, **kwargs) -> None:
        # *args/**kwargs: a host may pass callback kwargs positionally or by keyword;
        # neither is used here.
        if old != new:
            self._republish_destination()

    def _publish_destination_state(self, state: str, attrs: Dict[str, Any]) -> None:
        full = {"friendly_name": "VATSIM Airport ATIS", "airport": None, "role": None,
                "range_nm": self.destination_range_nm,
                "distance_nm": None, "atis_online": False, "atis_callsign": None, "atis_code": None,
                "atis_frequency": None, "atis_text": None, "atis_last_updated": None,
                "weather_source": "none", "metar_text": None, "metar_source": None,
                "metar_observed_at": None, "flight_category": None}
        full.update(attrs)
        # An alert on an ATIS letter change should test this, not only pilot_connected.
        # A failed presence-off write leaves that entity on while this one is accepted; a flag in
        # the same write as the letter cannot disagree with it. A string, because some hosts
        # drop False. It is in the dedupe key, so the change at connect and disconnect publishes.
        full["session"] = "connected" if self._presence_state == PresenceState.PRESENT else "offline"
        # distance_nm changes every poll in flight, so dedupe on everything else plus whole-nm distance:
        # one write per nautical mile instead of one per poll.
        d = full.get("distance_nm")
        key = (state, tuple(sorted((k, v) for k, v in full.items() if k != "distance_nm")),
               None if d is None else int(d))
        if key == self._last_destination_published:
            return
        self._last_destination_published = key
        self.set_state(DESTINATION_ENTITY_ID, state=state, attributes=full)

    def _publish_route_state(
        self, status: RouteResolutionStatus, departure: Optional[str], arrival: Optional[str],
        raw_route: Optional[str], resolved_count: int, unresolved_count: int,
        departure_resolved: bool, arrival_resolved: bool,
        geojson_str: Optional[str] = None, force: bool = False,
    ) -> None:
        attrs = {
            "friendly_name": "VATSIM Flight Route",
            "departure": departure,
            "arrival": arrival,
            "raw_route": self._cap_feed_text(raw_route),
            "resolved_waypoint_count": resolved_count,
            "unresolved_token_count": unresolved_count,
            "departure_airport_resolved": departure_resolved,
            "arrival_airport_resolved": arrival_resolved,
            "route_geojson": geojson_str,
        }
        snapshot = (status.value, tuple(sorted(attrs.items())))
        if snapshot == self._last_route_published and not force:
            return
        self._last_route_published = snapshot
        self.log(f"Publishing route resolution: status={status.value} "
                 f"departure={departure} arrival={arrival} "
                 f"resolved={resolved_count} unresolved={unresolved_count} "
                 f"departure_airport_resolved={departure_resolved} "
                 f"arrival_airport_resolved={arrival_resolved}", level="INFO")
        self.set_state(FLIGHT_ROUTE_ENTITY_ID, state=status.value, attributes=attrs)