"""Test harness: a stand-in for AppDaemon so the tracker imports and runs
outside the add-on. Only AppDaemon is replaced. requests, geopy and shapely
are the real packages:  pip install requests geopy shapely
"""
import importlib.util
import os
import sys
import types

APP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "apps", "vatsim_atc_tracker", "vatsim_atc_tracker.py")


class FakeHass:
    """The subset of hassapi.Hass the tracker calls. Records everything."""

    def __init__(self, args=None):
        self.args = dict(args or {})
        self.logs = []          # (level, message)
        self.states = {}        # entity_id -> (state, attributes) as last written
        self.writes = []        # every set_state call, in order
        self.services = []
        self.scheduled = []
        self.helper_states = {}

    def log(self, message, level="INFO", **_):
        self.logs.append((level, str(message)))

    def set_state(self, entity_id, **kwargs):
        self.states[entity_id] = (kwargs.get("state"), dict(kwargs.get("attributes") or {}))
        self.writes.append((entity_id, kwargs.get("state"), dict(kwargs.get("attributes") or {})))
        return kwargs

    def get_state(self, entity_id=None, **_):
        return self.helper_states.get(entity_id)

    def render_template(self, template, **_):
        return None  # "no answer from HA": the publish check then skips

    def call_service(self, service, **kwargs):
        self.services.append((service, kwargs))

    def run_in(self, callback, delay, **kwargs):
        # Zero-delay hand-offs (snapshot swaps) run inline; timers are recorded only.
        if delay == 0:
            callback(kwargs)
        else:
            self.scheduled.append(("in", getattr(callback, "__name__", "?"), delay))

    def run_every(self, callback, start, interval, **kwargs):
        self.scheduled.append(("every", getattr(callback, "__name__", "?"), interval))

    def datetime(self):
        import datetime
        return datetime.datetime.now()

    def errors(self):
        return [m for lvl, m in self.logs if lvl == "ERROR"]


def load_tracker(path=APP_PATH, module_name="vatsim_atc_tracker_under_test"):
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = FakeHass
    for name in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_app(module, **args):
    """A configured, state-initialised tracker that has not started any thread or timer."""
    args.setdefault("vatsim_cid", 1234567)
    app = module.VatsimAtcTracker(args)
    app._load_config()
    app._init_state()
    return app


# Synthetic navigation data in X-Plane 12 layout. Invented fixes on an invented
# airway; no real-world or subscription data.
FIX_DAT = """I
1200 Version - synthetic test data

 10.000000  20.000000 AAAAA ENRT ZZ 4530243
 10.000000  21.000000 BBBBB ENRT ZZ 4530243
 10.000000  22.000000 CCCCC ENRT ZZ 4530243
 10.000000  23.000000 DDDDD ENRT ZZ 4530243
-40.000000 150.000000 BBBBB ENRT YY 4530243
99
"""
NAV_DAT = """I
1200 Version - synthetic test data

3  11.000000  20.500000      100    11630   130      0.000  TVR ENRT ZZ TEST VOR
2 -33.900000  151.200000      20      400    50      0.000   TN ENRT YY TEST NDB
99
"""
AWY_DAT = """I
1100 Version - synthetic test data

AAAAA ZZ 11 BBBBB ZZ 11 N 2 180 450 T1
BBBBB ZZ 11 CCCCC ZZ 11 N 2 180 450 T1
CCCCC ZZ 11 DDDDD ZZ 11 N 2 180 450 T1-T2
99
"""
APTMETA_DAT = """I
1200 Version - synthetic test data

ZZAA ZZ   9.900000  19.900000 0 0 0
ZZDD ZZ  10.100000  23.100000 0 0 0
99
"""


def write_navdata(folder):
    for name, text in (("earth_fix.dat", FIX_DAT), ("earth_nav.dat", NAV_DAT),
                       ("earth_awy.dat", AWY_DAT), ("earth_aptmeta.dat", APTMETA_DAT)):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write(text)
