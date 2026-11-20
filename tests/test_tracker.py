"""Regression and fresh-install tests.  Run from the repo root:
    python -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest

from _harness import load_tracker, make_app, write_navdata

T = load_tracker()


class ConfigValidation(unittest.TestCase):
    def test_missing_cid_fails_loudly_and_names_the_key(self):
        app = T.VatsimAtcTracker({})
        with self.assertRaises(ValueError):
            app.initialize()
        self.assertEqual(len(app.errors()), 1)
        self.assertIn("vatsim_cid", app.errors()[0])
        self.assertEqual(app.scheduled, [], "no timer may be started after a config error")

    def test_placeholder_and_non_numeric_cid_rejected(self):
        for bad in ("your_cid", "REPLACE_ME", 0, "", "abc123"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                T.VatsimAtcTracker({"vatsim_cid": bad})._load_config()

    def test_bad_number_names_the_key(self):
        for key, bad in (("poll_interval_seconds", "fast"), ("lookahead_minutes", 0),
                         ("presence_absent_after_misses", 0), ("geometry_refresh_interval_days", -1),
                         ("track_history_max_points", None), ("stale_feed_multiplier", True)):
            app = T.VatsimAtcTracker({"vatsim_cid": 1234567, key: bad})
            with self.assertRaises(ValueError) as ctx:
                app._load_config()
            self.assertIn(f"'{key}'", str(ctx.exception))

    def test_defaults_match_documented_values(self):
        app = make_app(T)
        self.assertEqual((app.poll_interval, app.lookahead_minutes, app.min_groundspeed_kt), (18.0, 10.0, 30.0))
        self.assertEqual((app.heading_stability_window, app.heading_stability_max_delta_deg), (3, 20.0))
        self.assertEqual((app.departure_near_nm, app.takeoff_groundspeed_kt, app.destination_range_nm),
                         (5.0, 40.0, 100.0))
        self.assertIsNone(app.navdata_path)
        self.assertIsNone(app.simaware_boundaries_path)
        self.assertTrue(app._last_aircraft_path().endswith("vatsim_last_aircraft.json"))

    def test_blank_optional_strings_mean_unset(self):
        app = make_app(T, navdata_path="  ", nat_tracks_url="", vatsim_status_url=None,
                       last_aircraft_path="/tmp/x.json")
        self.assertIsNone(app.navdata_path)
        self.assertIsNone(app.nat_tracks_url)
        self.assertIsNone(app.vatsim_status_url)
        self.assertEqual(app._last_aircraft_path(), "/tmp/x.json")

    def test_poll_below_feed_interval_warns(self):
        app = make_app(T, poll_interval_seconds=5)
        self.assertTrue(any(lvl == "WARNING" and "poll_interval_seconds" in m for lvl, m in app.logs))


class NavdataParsers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        write_navdata(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_earth_nav_uses_lat_column_1(self):
        """Regression: earth_nav.dat leads with a row code. Reading latitude
        from column 0 turned the row code into a latitude."""
        rows = dict(T.VatsimAtcTracker._parse_navdata_coord_file(
            os.path.join(self.dir.name, "earth_nav.dat"), ident_column=7, lat_column=1))
        self.assertEqual(rows["TVR"], (11.0, 20.5))
        self.assertEqual(rows["TN"], (-33.9, 151.2))

    def test_range_guard_rejects_shifted_columns(self):
        """Regression: with the wrong lat_column the 'TN' row reads lat=2,
        lon=-33.9 (plausible, undetectable) but a row whose shifted values
        leave the valid range must be dropped, not loaded."""
        path = os.path.join(self.dir.name, "shifted.dat")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("151.200000 -33.900000 BADLAT\n 45.000000 181.000000 BADLON\n 45.000000  90.000000 GOOD1\n")
        rows = dict(T.VatsimAtcTracker._parse_navdata_coord_file(path, ident_column=2, lat_column=0))
        self.assertEqual(rows, {"GOOD1": (45.0, 90.0)})

    def test_earth_fix_and_headers(self):
        rows = T.VatsimAtcTracker._parse_navdata_coord_file(
            os.path.join(self.dir.name, "earth_fix.dat"), ident_column=2, lat_column=0)
        self.assertEqual(len(rows), 5)  # header, version and sentinel lines skipped
        self.assertIn(("BBBBB", (-40.0, 150.0)), rows)

    def test_aptmeta_is_ident_first(self):
        rows = dict(T.VatsimAtcTracker._parse_navdata_airport_file(
            os.path.join(self.dir.name, "earth_aptmeta.dat")))
        self.assertEqual(rows, {"ZZAA": (9.9, 19.9), "ZZDD": (10.1, 23.1)})

    def test_airways_split_shared_segment_names(self):
        graph = T.VatsimAtcTracker._parse_navdata_airways(os.path.join(self.dir.name, "earth_awy.dat"))
        self.assertEqual(sorted(graph), ["T1", "T2"])
        self.assertEqual(graph["T2"], {"CCCCC": ["DDDDD"], "DDDDD": ["CCCCC"]})


class RouteResolution(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        write_navdata(self.dir.name)
        self.app = make_app(T, navdata_path=self.dir.name, nat_tracks_url=None)
        self.app._load_navdata_worker_body()  # run_in(0) swaps the snapshot inline

    def tearDown(self):
        self.dir.cleanup()

    def test_fresh_install_navdata_loads_clean(self):
        self.assertEqual(self.app.errors(), [])
        self.assertIsNotNone(self.app._navdata)
        self.assertEqual(self.app._navdata.load_issue_count, 0)
        self.assertEqual(self.app._compute_navdata_status(), "active")

    def test_loader_reads_navaids_with_the_right_columns(self):
        """Regression at the call site: the loader must pass lat_column=1 for
        earth_nav.dat. With 0, TVR loads as (3.0, 11.0): the row code as latitude."""
        self.assertEqual(self.app._navdata.fix_coords["TVR"], ((11.0, 20.5),))
        self.assertEqual(self.app._navdata.fix_coords["TN"], ((-33.9, 151.2),))

    def test_airway_expansion_does_not_duplicate_exit_fix(self):
        """Regression: path_idents[1:-1]. With [1:] the exit fix of every
        airway segment appeared twice."""
        points, resolved, unresolved = self.app._resolve_route_to_points("AAAAA T1 DDDDD", self.app._navdata)
        self.assertEqual(points, [(10.0, 20.0), (10.0, 21.0), (10.0, 22.0), (10.0, 23.0)])
        self.assertEqual((resolved, unresolved), (2, 0))

    def test_duplicate_ident_resolves_to_the_near_one(self):
        points, _, _ = self.app._resolve_route_to_points("AAAAA DCT BBBBB", self.app._navdata)
        self.assertEqual(points[-1], (10.0, 21.0))  # not the BBBBB at -40, 150

    def test_speed_level_suffix_stripped_and_unknown_tokens_counted(self):
        points, resolved, unresolved = self.app._resolve_route_to_points(
            "SID1A AAAAA/N0450F350 DCT CCCCC STAR2B", self.app._navdata)
        self.assertEqual(points, [(10.0, 20.0), (10.0, 22.0)])
        self.assertEqual((resolved, unresolved), (2, 2))

    def test_route_publishes_with_airport_anchors(self):
        pilot = {"flight_plan": {"route": "AAAAA T1 DDDDD", "departure": "ZZAA", "arrival": "ZZDD"}}
        self.app._maybe_publish_route(pilot)
        state, attrs = self.app.states["sensor.vatsim_flight_route"]
        self.assertEqual(state, "ok")
        self.assertEqual(attrs["unresolved_token_count"], 0)
        self.assertTrue(attrs["departure_airport_resolved"] and attrs["arrival_airport_resolved"])
        geo = json.loads(attrs["route_geojson"])
        self.assertEqual(geo["type"], "LineString")
        coords = [tuple(c) for c in geo["coordinates"]]
        self.assertEqual(coords[0], (19.9, 9.9))    # GeoJSON order is lon, lat
        self.assertEqual(coords[-1], (23.1, 10.1))
        self.assertEqual(len(coords), 6)

    def test_snapshot_appliers_reset_route_key(self):
        """Regression: both appliers must clear _last_route_key, or a route
        resolved before the data arrived keeps its stale line until the
        filed plan changes."""
        self.app._last_route_key = ("X", "", "")
        self.app._apply_navdata_snapshot({"snapshot": self.app._navdata})
        self.assertIsNone(self.app._last_route_key)
        self.app._last_route_key = ("X", "", "")
        nat = T.NatTrackSnapshot(version=1, fix_coords={}, airway_graph={}, track_ids=(), fetched_at=0.0)
        self.app._apply_nat_track_snapshot({"snapshot": nat})
        self.assertIsNone(self.app._last_route_key)

    def test_nat_track_expands_like_an_airway(self):
        import time
        nat = T.NatTrackSnapshot(
            version=1, fix_coords={"50/30": ((50.0, -30.0),), "51/40": ((51.0, -40.0),)},
            airway_graph={"NATA": {"AAAAA": ("50/30",), "50/30": ("AAAAA", "51/40"),
                                   "51/40": ("50/30", "DDDDD"), "DDDDD": ("51/40",)}},
            track_ids=("A",), fetched_at=time.time(), valid_until=time.time() + 3600)
        self.app.nat_tracks_url = "https://example.invalid/tracks"
        self.app._nat = nat
        points, _, unresolved = self.app._resolve_route_to_points("AAAAA NATA DDDDD", self.app._navdata)
        self.assertEqual(points, [(10.0, 20.0), (50.0, -30.0), (51.0, -40.0), (10.0, 23.0)])
        self.assertEqual(unresolved, 0)
        self.app._nat = T.NatTrackSnapshot(version=2, fix_coords=nat.fix_coords, airway_graph=nat.airway_graph,
                                           track_ids=("A",), fetched_at=0.0, valid_until=time.time() - 1)
        self.assertEqual(self.app._compute_nat_track_status(), "expired")
        points, _, unresolved = self.app._resolve_route_to_points("AAAAA NATA DDDDD", self.app._navdata)
        self.assertEqual(points, [(10.0, 20.0), (10.0, 23.0)], "an expired track must draw nothing")


class BadNavdataPath(unittest.TestCase):
    def _run(self, path):
        app = make_app(T, navdata_path=path, nat_tracks_url=None)
        app._load_navdata_worker_body()
        return app

    def test_missing_directory_is_an_error_with_path_and_file_names(self):
        app = self._run("/config/does/not/exist")
        errors = app.errors()
        self.assertEqual(len(errors), 1)
        self.assertIn(os.path.abspath("/config/does/not/exist"), errors[0])
        for name in ("earth_fix.dat", "earth_nav.dat", "earth_awy.dat", "earth_aptmeta.dat"):
            self.assertIn(name, errors[0])
        self.assertIn("/homeassistant", errors[0])
        self.assertIsNone(app._navdata, "an empty snapshot must not be swapped in")
        self.assertEqual(app._compute_navdata_status(), "configured_but_unloaded")
        self.assertIsNotNone(app._last_navdata_refresh_error)

    def test_empty_directory_is_an_error(self):
        with tempfile.TemporaryDirectory() as empty:
            app = self._run(empty)
            self.assertEqual(len(app.errors()), 1)
            self.assertIn("contains none of the expected files", app.errors()[0])

    def test_error_reaches_feed_health(self):
        app = self._run("/config/does/not/exist")
        app._publish_feed_health(T.FeedHealth.HEALTHY, 1000.0, feed_age=5.0, timestamp_status="ok")
        _, attrs = app.states["sensor.vatsim_feed_health"]
        self.assertEqual(attrs["navdata_status"], "configured_but_unloaded")
        self.assertIn("navdata_path", attrs["last_navdata_refresh_error"])

    def test_change_check_retries_when_nothing_is_loaded(self):
        app = self._run("/config/does/not/exist")
        started = []
        app._start_navdata_worker = lambda name: started.append(name)
        app._check_navdata_changed({})
        self.assertEqual(started, ["vatsim-navdata-retry"])


class FeedUrlLookup(unittest.TestCase):
    class _Resp:
        def __init__(self, doc): self._doc = doc
        def raise_for_status(self): pass
        def json(self): return self._doc

    def _lookup(self, doc=None, exc=None, **args):
        app = make_app(T, **args)
        calls = []
        def fake_get(url, **kwargs):
            calls.append((url, kwargs))
            if exc: raise exc
            return self._Resp(doc)
        original, T.requests.get = T.requests.get, fake_get
        try:
            app._resolve_feed_url()
        finally:
            T.requests.get = original
        return app, calls

    def test_url_taken_from_status_document_with_user_agent(self):
        app, calls = self._lookup({"data": {"v3": ["https://data2.vatsim.net/v3/vatsim-data.json"]}})
        self.assertEqual(app._feed_url, "https://data2.vatsim.net/v3/vatsim-data.json")
        self.assertEqual(calls[0][0], "https://status.vatsim.net/status.json")
        agent = calls[0][1]["headers"]["User-Agent"]
        self.assertTrue(agent.startswith("vatsim_ha_tracker/"))
        self.assertNotIn("1234567", agent)

    def test_foreign_host_malformed_or_failed_lookup_keeps_default(self):
        for doc, exc in (({"data": {"v3": ["https://evil.example/v3/vatsim-data.json"]}}, None),
                         ({"data": {"v3": ["http://data.vatsim.net/v3/vatsim-data.json"]}}, None),
                         ({"data": {}}, None), ([], None), (None, OSError("down"))):
            app, _ = self._lookup(doc, exc)
            self.assertEqual(app._feed_url, T.VATSIM_FEED_URL)
            self.assertTrue(any(lvl == "WARNING" for lvl, _m in app.logs))

    def test_lookup_disabled(self):
        app, calls = self._lookup({"data": {"v3": ["https://x.vatsim.net/a"]}}, vatsim_status_url="")
        self.assertEqual(calls, [])
        self.assertEqual(app._feed_url, T.VATSIM_FEED_URL)


class SmallParsers(unittest.TestCase):
    def test_nat_oceanic_point(self):
        f = T.VatsimAtcTracker._parse_nat_oceanic_point
        self.assertEqual(f("56/20"), (56.0, -20.0))
        self.assertIsNone(f("RESNO"))
        self.assertIsNone(f("95/20"))

    def test_last_aircraft_file_round_trip_and_cid_check(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "last.json")
            app = make_app(T, last_aircraft_path=path)
            app._last_aircraft_attrs = {"latitude": 1.5, "longitude": 2.5, "cid": "1234567", "callsign": "TEST1"}
            app._persist_last_aircraft(force=True)
            self.assertEqual(make_app(T, last_aircraft_path=path)._load_last_aircraft()["latitude"], 1.5)
            other = make_app(T, vatsim_cid=7654321, last_aircraft_path=path)
            self.assertIsNone(other._load_last_aircraft(), "a save file from another CID must be ignored")


if __name__ == "__main__":
    unittest.main()
