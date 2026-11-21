"""Test support: a fake host for the engine, and a way to import the integration's
Home Assistant-free modules without Home Assistant installed."""
import importlib
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PACKAGE_DIR = os.path.join(ROOT, "custom_components", "vatsim_atc")
PACKAGE = "vatsim_atc_pkg"

import logging  # noqa: E402
logging.getLogger(PACKAGE).addHandler(logging.NullHandler())  # the host logs through this name


def load(module):
    """Import custom_components/vatsim_atc/<module> without running the package's __init__.py,
    which imports Home Assistant. load("core.engine"), load("host"), load("geo"), load("const")."""
    if PACKAGE not in sys.modules:
        package = types.ModuleType(PACKAGE)
        package.__path__ = [PACKAGE_DIR]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.{module}")


class FakeHost:
    """The Host contract, in memory. Timers with delay 0 run at once (the engine uses them to
    hand a loader thread's result back); every other timer is recorded and not run. Set .fetch
    on the class to a function (url, timeout=, headers=) -> text."""

    fetch = None

    def __init__(self, args=None):
        self.args = dict(args or {})
        self.logs, self.states, self.writes, self.scheduled, self.listening = [], {}, [], [], []
        self.every = []   # (callback name, seconds until the first run, interval)

    def log(self, message, level="INFO", **_):
        self.logs.append((level, str(message)))

    def run_in(self, callback, delay, **kwargs):
        if delay == 0:
            callback(kwargs)
        else:
            self.scheduled.append((getattr(callback, "__name__", "?"), delay))

    def run_every(self, callback, start, interval, **kwargs):
        self.scheduled.append((getattr(callback, "__name__", "?"), interval))
        self.every.append((getattr(callback, "__name__", "?"), (start - self.datetime()).total_seconds(), interval))

    def listen_state(self, callback, entity_id, **kwargs):
        self.listening.append(entity_id)

    def set_state(self, entity_id, **kwargs):
        record = (kwargs.get("state"), dict(kwargs.get("attributes") or {}))
        self.states[entity_id] = record
        self.writes.append((entity_id,) + record)
        return kwargs

    def get_state(self, entity_id=None, **kwargs):
        return None

    def render_template(self, template, **kwargs):
        return None

    def call_service(self, service, **kwargs):
        pass

    def datetime(self):
        import datetime
        return datetime.datetime.now()

    def http_get(self, url, timeout, headers=None):
        if type(self).fetch is None:
            raise RuntimeError("FakeHost.fetch is not set")
        return type(self).fetch(url, timeout=timeout, headers=headers)

    def errors(self):
        return [m for lvl, m in self.logs if lvl == "ERROR"]


def make_engine(fetch=None, **args):
    """The engine on a fresh FakeHost subclass, engine first (see Host in core/engine.py)."""
    engine = load("core.engine")
    host = type("Host_", (FakeHost,), {"fetch": staticmethod(fetch) if fetch else None})
    tracker = type("Tracker", (engine.VatsimAtcEngine, host), {})
    args.setdefault("vatsim_cid", 1234567)
    app = tracker(args)
    app._load_config()
    app._init_state()
    # The engine's default save file is next to its own source. Tests must not write there.
    app._load_last_aircraft = lambda: None
    app._persist_last_aircraft = lambda force=False: None
    return app
