"""Constants for the VATSIM ATC Tracker integration. No Home Assistant imports."""

DOMAIN = "vatsim_atc"
VERSION = "2.0.0b11"
PROJECT_URL = "https://github.com/SavBuilds/vatsim_ha_tracker"
USER_AGENT = f"vatsim_ha_tracker/{VERSION} (+{PROJECT_URL})"

CONF_CID = "vatsim_cid"
CONF_NAVDATA_PATH = "navdata_path"
CONF_SIMAWARE_PATH = "simaware_boundaries_path"
CONF_LOOKAHEAD_MINUTES = "lookahead_minutes"
CONF_METAR_FALLBACK = "metar_fallback"

# Option keys are the engine's own config keys, so options pass straight through.
OPTION_KEYS = (CONF_NAVDATA_PATH, CONF_SIMAWARE_PATH, CONF_LOOKAHEAD_MINUTES, CONF_METAR_FALLBACK)

# Alert options. These stay in the integration; the engine never sees them.
CONF_NOTIFY_TARGETS = "notify_targets"
CONF_SPEAKERS = "speakers"
CONF_TTS_ENTITY = "tts_entity"
CONF_MUTE_GATE = "mute_gate_entity"
CONF_PUSH_REPAIRS = "push_repairs"
ALERT_OPTION_KEYS = (CONF_NOTIFY_TARGETS, CONF_SPEAKERS, CONF_TTS_ENTITY, CONF_MUTE_GATE, CONF_PUSH_REPAIRS)
# A push target is a notify action ("service:<action name>") or a notify entity
# ("entity:<entity id>"). The prefix says which, because the two share one namespace.
SERVICE_TARGET_PREFIX = "service:"
ENTITY_TARGET_PREFIX = "entity:"
SIDEBAR_TARGET = SERVICE_TARGET_PREFIX + "persistent_notification"
# Until the user chooses: a notice in the sidebar, which every install has.
DEFAULT_NOTIFY_TARGETS = (SIDEBAR_TARGET,)

DEVICE_NAME = "VATSIM Tracker"

# Engine attributes that never become entity attributes: the first two are plumbing for
# hosts that publish from outside Home Assistant, the rest are shown through the device
# tracker's own properties.
HIDDEN_ATTRIBUTES = frozenset({"friendly_name", "pub_token"})
TRACKER_PROPERTY_ATTRIBUTES = frozenset({"latitude", "longitude", "source_type", "gps_accuracy"})

# Map layers and the raw track points. Large, and useless as history, so the recorder skips them.
UNRECORDED_ATTRIBUTES = frozenset({
    "all_boundaries_geojson", "boundary_geojson", "next_boundary_geojson", "route_geojson", "track_geojson",
    "points",
})

# Attributes that are always present on their entity, null when there is nothing to report.
# Every other attribute is left out while it has no value. These are the ones the alerts
# package triggers on or reads, and tools that check an automation's attribute names against
# the entity (Spook, for one) report a missing key as an error whenever no one is connected.
# Keys are the engine's own state names.
STABLE_ATTRIBUTES = {
    "binary_sensor.vatsim_atc_coverage": ("controller_callsign",),
    "sensor.vatsim_next_sector": ("controller_callsign",),
    "sensor.vatsim_destination": ("atis_code",),
}


def signal_for(entry_id: str, source_id: str) -> str:
    """Dispatcher signal sent when the engine publishes source_id."""
    return f"{DOMAIN}_{entry_id}_{source_id}"


def engine_args(data: dict, options: dict) -> dict:
    """Config entry data and options as the engine's args dict. Blank options are left out so
    the engine applies its own defaults."""
    args = {CONF_CID: str(data[CONF_CID]).strip()}
    for key in OPTION_KEYS:
        value = options.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        args[key] = value.strip() if isinstance(value, str) else value
    return args
