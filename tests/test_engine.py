"""The engine end to end on an invented world, through a fake host. No network."""
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario as a  # noqa: E402
from _harness import PACKAGE_DIR, load, make_engine  # noqa: E402

engine = load("core.engine")


class Engine(unittest.TestCase):
    def setUp(self):
        self.payload = a.feed(23.0)
        self.app = make_engine(fetch=self._fetch, vatspy_boundaries_url=a.BOUNDARIES_URL,
                               vatspy_dat_url=a.DAT_URL, metar_fallback=False)
        self.app._load_last_aircraft = lambda: None
        self.app._persist_last_aircraft = lambda force=False: None
        self.app._load_geometry_worker()

    def _fetch(self, url, timeout, headers=None):
        return a.serve(url, self.payload)

    def poll(self, times=1):
        for _ in range(times):
            self.app._run_poll_cycle_body()

    def test_imports_nothing_it_should_not(self):
        with open(os.path.join(PACKAGE_DIR, "core", "engine.py"), encoding="utf-8") as fh:
            source = fh.read()
        for banned in ("import requests", "import appdaemon", "shapely", "geopy", "homeassistant"):
            self.assertNotIn(banned, source)

    def test_coverage_and_lookahead(self):
        self.poll(4)
        self.assertEqual(self.app.errors(), [])
        state, attrs = self.app.states["binary_sensor.vatsim_atc_coverage"]
        self.assertEqual((state, attrs["controller_callsign"], attrs["polygon_id"]), ("on", "ZZA_CTR", "ZZAA"))
        state, attrs = self.app.states["sensor.vatsim_next_sector"]
        self.assertEqual(attrs["next_controller_callsign"], "ZZB_CTR")
        self.assertEqual(attrs["next_result_kind"], "different_sector")
        # One degree of longitude at latitude 10 on the sphere the geometry uses.
        expected_nm = math.radians(1) * math.cos(math.radians(10)) * 3440.0695
        self.assertAlmostEqual(attrs["eta_distance_nm"], expected_nm, delta=0.15)
        self.assertAlmostEqual(attrs["eta_minutes"], expected_nm / 480 * 60, delta=0.1)
        self.assertEqual(attrs["eta_text"], "7 min")

    def test_outlines(self):
        self.poll(4)
        current = json.loads(self.app.states["binary_sensor.vatsim_atc_coverage"][1]["boundary_geojson"])
        nxt = json.loads(self.app.states["sensor.vatsim_next_sector"][1]["next_boundary_geojson"])
        self.assertEqual(current, {"type": "Polygon", "coordinates": a.square(20, 8, 24, 12)})
        self.assertEqual(nxt, {"type": "Polygon", "coordinates": a.square(24, 8, 28, 12)})

    def test_online_boundaries_layer(self):
        self.poll(4)
        _state, attrs = self.app.states["sensor.vatsim_online_atc_boundaries"]
        layer = json.loads(attrs["all_boundaries_geojson"])
        self.assertEqual(sorted(f["properties"]["boundary_id"] for f in layer["features"]), ["ZZAA", "ZZBB"])

    def test_unusable_boundary_is_unresolved_not_fatal(self):
        self.payload = a.feed(23.0, controllers=("ZZA_CTR", "ZZC_CTR"))
        self.poll(4)
        self.assertEqual(self.app.errors(), [])
        self.assertEqual(self.app.states["binary_sensor.vatsim_atc_coverage"][1]["controller_callsign"], "ZZA_CTR")
        self.assertTrue(any("ZZCC" in m and "unresolved" in m for lvl, m in self.app.logs if lvl == "WARNING"))

    def test_uncontrolled_ahead(self):
        self.payload = a.feed(23.0, controllers=("ZZA_CTR",))
        self.poll(4)
        self.assertEqual(self.app.states["sensor.vatsim_next_sector"][1]["next_result_kind"],
                         "projected_uncontrolled")

    def test_tracker_and_session_track(self):
        self.poll(2)
        _state, attrs = self.app.states["device_tracker.vatsim_aircraft"]
        self.assertEqual((attrs["latitude"], attrs["longitude"]), (10.0, 23.0))
        self.assertIn("sensor.vatsim_session_track", self.app.states)

    def test_feed_failure_is_reported_not_raised(self):
        def broken(url, timeout, headers=None):
            raise engine.FetchError("connection refused", kind="ConnectionError")
        type(self.app).fetch = staticmethod(broken)
        self.poll(3)
        state, attrs = self.app.states["sensor.vatsim_feed_health"]
        self.assertEqual(state, "unavailable")
        self.assertEqual(attrs["consecutive_failures"], 3)
        self.assertTrue(any("request error: connection refused" in m for _l, m in self.app.logs))

    def test_feed_timeout_is_named(self):
        def slow(url, timeout, headers=None):
            raise engine.FetchError("no answer in 10 s", kind="ReadTimeout", is_timeout=True)
        type(self.app).fetch = staticmethod(slow)
        self.poll(1)
        self.assertTrue(any("read timeout: no answer in 10 s" in m for _l, m in self.app.logs))

    def test_feed_that_is_not_json(self):
        type(self.app).fetch = staticmethod(lambda url, timeout, headers=None: "<html>busy</html>")
        self.poll(1)
        self.assertTrue(any("invalid JSON" in m for _l, m in self.app.logs))

    def test_boundary_download_failure_keeps_the_error_name(self):
        def refuse(url, timeout, headers=None):
            if url == a.BOUNDARIES_URL:
                raise engine.FetchError("refused", kind="ConnectionError")
            return a.serve(url, self.payload)
        app = make_engine(fetch=refuse, vatspy_boundaries_url=a.BOUNDARIES_URL, vatspy_dat_url=a.DAT_URL,
                          metar_fallback=False)
        app._load_last_aircraft = lambda: None
        app._persist_last_aircraft = lambda force=False: None
        app._load_geometry_worker()
        app._run_poll_cycle_body()
        attrs = app.states["sensor.vatsim_feed_health"][1]
        self.assertEqual(attrs["geometry_status"], "loading")
        self.assertEqual(attrs["last_geometry_refresh_error"], "ConnectionError: refused")

    def test_host_contract_is_complete(self):
        used = {"log", "set_state", "get_state", "render_template", "call_service", "run_in", "run_every",
                "listen_state", "datetime", "http_get"}
        self.assertTrue(used <= set(vars(engine.Host)))

    def test_bad_cid_is_refused(self):
        with self.assertRaises(ValueError):
            make_engine(vatsim_cid="replace_me")


if __name__ == "__main__":
    unittest.main()
