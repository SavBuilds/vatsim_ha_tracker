"""Constants for the VATSIM ATC Tracker integration. No Home Assistant imports."""

DOMAIN = "vatsim_atc"
VERSION = "2.0.0b6"
PROJECT_URL = "https://github.com/SavBuilds/vatsim_ha_tracker"
USER_AGENT = f"vatsim_ha_tracker/{VERSION} (+{PROJECT_URL})"

CONF_CID = "vatsim_cid"
CONF_NAVDATA_PATH = "navdata_path"
CONF_SIMAWARE_PATH = "simaware_boundaries_path"
CONF_LOOKAHEAD_MINUTES = "lookahead_minutes"
CONF_METAR_FALLBACK = "metar_fallback"

# Option keys are the engine's own config keys, so options pass straight through.
OPTION_KEYS = (CONF_NAVDATA_PATH, CONF_SIMAWARE_PATH, CONF_LOOKAHEAD_MINUTES, CONF_METAR_FALLBACK)

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
