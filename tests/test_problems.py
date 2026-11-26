"""The rules that decide which problems are reported (core/problems.py). No Home Assistant."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import PACKAGE_DIR, load  # noqa: E402

problems = load("core.problems")
T0 = 5_000.0               # the monotonic clock: unrelated to the date
WALL = 1_000_000.0         # the wall clock the engine stamps last_success_ts with
HEALTHY = {"last_success_ts": WALL, "consecutive_failures": 0, "timestamp_status": "ok",
           "geometry_status": "ready", "navdata_status": "active", "navdata_load_issue_count": 0}


class Rules(unittest.TestCase):
    def setUp(self):
        self.watch = problems.ProblemWatch(started=T0)

    def run_for(self, seconds, attrs, connected=False, step=30, fresh=True, start=0):
        """Evaluate every `step` seconds, as the integration's timer does. Times are the
        monotonic clock's. fresh=True moves last_success_ts on each time, the way a working
        feed would; the engine's health check is always current."""
        active = {}
        for elapsed in range(start, start + seconds + 1, step):
            now = T0 + elapsed
            current = dict(attrs)
            if fresh and current.get("last_success_ts"):
                current["last_success_ts"] = WALL + elapsed
            active = self.watch.evaluate(now, ("healthy", current), connected, now)
        return active

    def test_a_healthy_tracker_reports_nothing(self):
        self.assertEqual(self.run_for(3600, HEALTHY), {})
        self.assertEqual(self.run_for(3600, HEALTHY, connected=True, start=3600), {})

    def test_nothing_is_reported_before_the_first_health_check_until_the_grace_time_is_up(self):
        self.assertEqual(self.watch.evaluate(T0 + 299, None, False, None), {})
        self.assertEqual(self.watch.evaluate(T0 + 301, None, False, None), {})     # one quiet reading
        self.assertEqual(self.watch.evaluate(T0 + 331, None, False, None), {problems.TRACKER_STALLED: {}})

    def test_one_quiet_reading_is_not_a_stopped_tracker(self):
        # R20: the reporter can read fresh health together with the time of the check before
        # it. That lasts an instant, so a stall counts only when two checks in a row see it.
        expired = dict(HEALTHY, navdata_status="expired", navdata_days_expired=30, navdata_cycle="2001", navdata_valid_until="2020-01-29")
        before = self.run_for(60, expired)
        now = T0 + 5000
        self.assertEqual(self.watch.evaluate(now, ("healthy", expired), False, now - 301), before)
        fresh = dict(expired, last_success_ts=WALL + 1)
        self.assertEqual(self.watch.evaluate(now + 30, ("healthy", fresh), False, now + 30), before)
        self.assertEqual(self.watch.evaluate(now + 60, ("healthy", fresh), False, now - 301), before)
        self.assertIn(problems.TRACKER_STALLED, self.watch.evaluate(now + 90, ("healthy", fresh), False, now - 301))

    def test_feed_unreachable_needs_ten_minutes_of_nothing_but_failures(self):
        failing = {"consecutive_failures": 3, "geometry_status": "ready"}     # no last_success_ts at all
        self.assertEqual(self.run_for(570, failing), {})
        self.assertEqual(list(self.run_for(30, failing, start=600)), [problems.FEED_UNREACHABLE])
        self.assertEqual(self.run_for(30, HEALTHY, start=660), {})            # first success clears it

    def test_feed_unreachable_starts_again_after_a_gap(self):
        failing = {"consecutive_failures": 3, "geometry_status": "ready"}
        self.run_for(300, failing)
        self.run_for(30, HEALTHY, start=330)
        self.assertEqual(self.run_for(300, failing, start=390), {})           # the earlier 5 min do not count

    def test_feed_stale_is_quicker_while_connected(self):
        old = dict(HEALTHY, consecutive_failures=5)
        for connected, limit in ((True, problems.STALE_CONNECTED_SECONDS), (False, problems.STALE_IDLE_SECONDS)):
            self.watch = problems.ProblemWatch(started=T0)
            self.assertEqual(self.run_for(limit, old, connected, fresh=False), {}, connected)
            self.assertEqual(list(self.run_for(0, old, connected, fresh=False, start=limit + 30)),
                             [problems.FEED_STALE], connected)
            self.assertEqual(self.run_for(0, dict(old, last_success_ts=WALL + 9), connected, fresh=False,
                                          start=limit + 60), {}, connected)    # a new download clears it
        self.assertLess(problems.STALE_CONNECTED_SECONDS, problems.STALE_IDLE_SECONDS)

    def test_wall_clock_steps_change_nothing(self):
        # R16. last_success_ts is a wall-clock value from the engine. Whatever it reads against
        # "now", only its changing matters: an hour ahead or behind raises nothing while it
        # keeps moving, and does not hide a feed that stopped.
        for skew in (3600.0, -3600.0, 0.0):
            self.watch = problems.ProblemWatch(started=T0)
            moving = dict(HEALTHY, last_success_ts=WALL + skew)
            for elapsed in range(0, 1801, 30):
                moving["last_success_ts"] += 15
                self.assertEqual(self.watch.evaluate(T0 + elapsed, ("healthy", moving), True, T0 + elapsed), {}, skew)
            stopped = self.run_for(210, moving, connected=True, fresh=False, start=1830)
            self.assertEqual(list(stopped), [problems.FEED_STALE], skew)

    def test_a_stopped_engine_is_reported_and_what_was_already_reported_stays(self):
        # R19: expired navdata must not be cleared by a stall and raised again after it.
        expired = dict(HEALTHY, navdata_status="expired", navdata_days_expired=30, navdata_cycle="2001", navdata_valid_until="2020-01-29")
        before = self.run_for(60, expired)
        self.assertEqual(list(before), [problems.NAVDATA_EXPIRED])
        now = T0 + 5000
        for at in (now - 30, now):
            during = self.watch.evaluate(at, ("healthy", expired), False, now - 30 - problems.STALLED_SECONDS - 1)
        self.assertEqual(during, {**before, problems.TRACKER_STALLED: {}})
        after = self.watch.evaluate(now + 30, ("healthy", dict(expired, last_success_ts=WALL + 1)), False, now + 30)
        self.assertEqual(after, before)

    def test_a_stall_does_not_report_what_was_not_reported_before(self):
        loading = dict(HEALTHY, geometry_status="loading", last_geometry_refresh_error="X")
        self.run_for(60, loading)                                              # inside its hold
        now = T0 + 5000
        for at in (now - 30, now):
            stalled = self.watch.evaluate(at, ("healthy", loading), True, now - 30 - problems.STALLED_SECONDS - 1)
        self.assertEqual(stalled, {problems.TRACKER_STALLED: {}})

    def test_health_that_is_never_checked_is_a_stopped_tracker_whatever_else_is_published(self):
        # R14: the caller passes the engine's health heartbeat, not the time of any publish.
        # With health absent and the heartbeat absent, the grace time ends in a report.
        self.watch.evaluate(T0 + 3570, None, True, None)
        self.assertEqual(self.watch.evaluate(T0 + 3600, None, True, None), {problems.TRACKER_STALLED: {}})
        # With health present but its heartbeat old, the same.
        self.assertIn(problems.TRACKER_STALLED,
                      self.watch.evaluate(T0 + 3630, ("healthy", HEALTHY), True, T0 + 3600 - 301))

    def test_failures_need_five_minutes(self):
        cases = ((problems.PROCESSING_ERROR, dict(HEALTHY, timestamp_status="processing_error")),
                 (problems.BOUNDARIES_NOT_LOADED, dict(HEALTHY, geometry_status="loading",
                                                       last_geometry_refresh_error="FetchError: down")),
                 (problems.NAVDATA_NOT_LOADED, dict(HEALTHY, navdata_status="configured_but_empty")),
                 (problems.NAVDATA_NOT_LOADED, dict(HEALTHY, navdata_status="configured_but_unloaded",
                                                    last_navdata_refresh_error="OSError: gone")),
                 (problems.NAVDATA_NOT_LOADED, dict(HEALTHY, navdata_load_issue_count=2)))
        for key, attrs in cases:
            self.watch = problems.ProblemWatch(started=T0)
            self.assertEqual(self.run_for(270, attrs), {}, key)
            self.assertEqual(list(self.run_for(30, attrs, start=300)), [key], attrs)
            self.assertEqual(self.run_for(30, HEALTHY, start=360), {}, key)

    def test_a_load_still_running_with_no_error_is_given_thirty_minutes(self):
        # R15: "loading" and "configured_but_unloaded" are also what a healthy start reads.
        cases = ((problems.BOUNDARIES_NOT_LOADED, dict(HEALTHY, geometry_status="loading")),
                 (problems.NAVDATA_NOT_LOADED, dict(HEALTHY, navdata_status="configured_but_unloaded")))
        self.assertGreater(problems.HOLD_STILL_LOADING, problems.HOLD_BOUNDARIES)
        for key, attrs in cases:
            self.watch = problems.ProblemWatch(started=T0)
            self.assertEqual(self.run_for(problems.HOLD_STILL_LOADING - 30, attrs), {}, key)
            self.assertEqual(list(self.run_for(30, attrs, start=problems.HOLD_STILL_LOADING)), [key], key)
            self.assertEqual(self.run_for(30, HEALTHY, start=problems.HOLD_STILL_LOADING + 60), {}, key)

    def test_an_error_during_a_slow_load_is_reported_five_minutes_after_the_error(self):
        loading = dict(HEALTHY, geometry_status="loading")
        self.assertEqual(self.run_for(600, loading), {})
        failed = dict(loading, last_geometry_refresh_error="FetchError: down")
        self.assertEqual(self.run_for(270, failed, start=630), {})
        self.assertEqual(list(self.run_for(30, failed, start=930)), [problems.BOUNDARIES_NOT_LOADED])
        self.watch = problems.ProblemWatch(started=T0)                         # and the same for navdata
        loading = dict(HEALTHY, navdata_status="configured_but_unloaded")
        self.assertEqual(self.run_for(600, loading), {})
        failed = dict(loading, last_navdata_refresh_error="OSError: gone")
        self.assertEqual(self.run_for(270, failed, start=630), {})
        self.assertEqual(list(self.run_for(30, failed, start=930)), [problems.NAVDATA_NOT_LOADED])

    def test_a_failed_navdata_reload_is_reported_while_the_older_data_stays_active(self):
        # R17: status "active", no load issues, but the reload raised.
        attrs = dict(HEALTHY, last_navdata_refresh_error="UnicodeDecodeError: bad byte")
        self.assertEqual(self.run_for(270, attrs), {})
        self.assertEqual(self.run_for(30, attrs, start=300), {problems.NAVDATA_NOT_LOADED: {
            "status": "active", "error": "UnicodeDecodeError: bad byte"}})

    def test_expired_navdata_is_reported_with_its_cycle_and_last_day(self):
        expired = dict(HEALTHY, navdata_status="expired", navdata_days_expired=30, navdata_cycle="2001", navdata_valid_until="2020-01-29")
        self.assertEqual(self.run_for(0, expired),
                         {problems.NAVDATA_EXPIRED: {"cycle": "2001", "valid_until": "2020-01-29"}})

    def test_expired_navdata_is_not_reported_on_the_first_day_after_its_last(self):
        for days, reported in ((0, False), (1, False), (2, True), (7, True), (40, True), (None, False)):
            self.watch = problems.ProblemWatch(started=T0)
            attrs = dict(HEALTHY, navdata_status="expired", navdata_cycle="2001", navdata_valid_until="2020-01-29")
            attrs["navdata_days_expired"] = days
            self.assertEqual(problems.NAVDATA_EXPIRED in self.run_for(600, attrs), reported, days)
        self.assertEqual(problems.NAVDATA_EXPIRED_GRACE_DAYS, 2)

    def test_disabled_navdata_and_a_missing_geometry_status_are_not_problems(self):
        self.assertEqual(self.run_for(3600, dict(HEALTHY, navdata_status="disabled")), {})
        without = {k: v for k, v in HEALTHY.items() if k != "geometry_status"}
        self.assertEqual(self.run_for(3600, without), {})

    def test_error_text_is_carried_and_bounded(self):
        attrs = dict(HEALTHY, geometry_status="loading", last_geometry_refresh_error="E" * 5000)
        text = self.run_for(330, attrs)[problems.BOUNDARIES_NOT_LOADED]["error"]
        self.assertEqual(len(text), 300)
        self.watch = problems.ProblemWatch(started=T0)
        none = self.run_for(problems.HOLD_STILL_LOADING + 30, dict(HEALTHY, geometry_status="loading"))
        self.assertEqual(none[problems.BOUNDARIES_NOT_LOADED]["error"], "no error was recorded")

    def test_placeholders_are_strings_and_stable_between_checks(self):
        attrs = dict(HEALTHY, geometry_status="loading", last_geometry_refresh_error="X",
                     navdata_status="expired", navdata_days_expired=30, navdata_cycle=2001, consecutive_failures=1)
        first = self.run_for(330, attrs)
        attrs["consecutive_failures"] = 99                    # a counter that moves every poll
        second = self.run_for(30, attrs, start=360)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        for placeholders in first.values():
            self.assertTrue(all(isinstance(v, str) for v in placeholders.values()))


class RulesOutsideTheEngine(unittest.TestCase):
    """The four problems added with the alerts: METAR, the saved position, the entity that
    holds speech, and a failed delivery."""

    def setUp(self):
        self.watch = problems.ProblemWatch(started=T0)

    def at(self, elapsed, attrs=HEALTHY, alerts=None, check=True):
        now = T0 + elapsed
        current = dict(attrs, last_success_ts=WALL + elapsed)
        return self.watch.evaluate(now, ("healthy", current), False, now if check else T0, alerts)

    def test_metar_failing_needs_fifteen_minutes_and_clears_when_no_metar_is_wanted(self):
        failing = dict(HEALTHY, metar_status="error", last_metar_error="FetchError: 503")
        for elapsed in range(0, problems.HOLD_METAR, 30):
            self.assertEqual(self.at(elapsed, failing), {}, elapsed)
        self.assertEqual(self.at(problems.HOLD_METAR, failing),
                         {problems.METAR_FAILING: {"error": "FetchError: 503"}})
        self.assertEqual(self.at(problems.HOLD_METAR + 30, dict(HEALTHY, metar_status="idle")), {})

    def test_a_saved_position_that_could_not_be_read_is_reported_and_no_file_is_not(self):
        self.assertEqual(self.at(0, dict(HEALTHY, last_aircraft_restore_error=None)), {})
        self.assertEqual(self.at(30, dict(HEALTHY, last_aircraft_restore_error="ValueError: not JSON")),
                         {problems.POSITION_NOT_RESTORED: {"error": "ValueError: not JSON"}})
        self.assertEqual(self.at(60), {})

    def test_the_hold_entity_is_reported_only_when_broken_for_fifteen_minutes(self):
        for state in ("on", "off"):
            self.watch = problems.ProblemWatch(started=T0)
            for elapsed in (0, 3600):
                self.assertEqual(self.at(elapsed, alerts={"gate": ("switch.example_hold", state)}), {}, state)
        self.assertEqual(self.at(3630, alerts={"gate": None}), {})
        broken = {"gate": ("switch.example_hold", "unavailable")}
        self.watch = problems.ProblemWatch(started=T0)
        for elapsed in range(0, problems.HOLD_MUTE_GATE, 30):
            self.assertEqual(self.at(elapsed, alerts=broken), {}, elapsed)
        self.assertEqual(self.at(problems.HOLD_MUTE_GATE, alerts=broken), {
            problems.MUTE_GATE_UNAVAILABLE: {"entity": "switch.example_hold", "state": "unavailable"}})
        self.assertEqual(self.at(problems.HOLD_MUTE_GATE + 30, alerts={"gate": ("switch.example_hold", "off")}), {})

    def test_a_failed_delivery_is_reported_at_once_and_until_one_works(self):
        failed = {"gate": None, "delivery_error": "push to mobile_app_example: ServiceNotFound"}
        self.assertEqual(self.at(0, alerts=failed), {
            problems.ALERT_DELIVERY_FAILED: {"error": "push to mobile_app_example: ServiceNotFound"}})
        self.assertEqual(self.at(30, alerts={"gate": None, "delivery_error": None}), {})

    def test_a_stopped_engine_does_not_hide_these(self):
        # They do not come from the engine's health, so they are judged while it is silent.
        failed = {"gate": None, "delivery_error": "speech on media_player.example: timeout"}
        self.at(0)
        for elapsed in (400, 430):
            found = self.at(elapsed, alerts=failed, check=False)
        self.assertEqual(sorted(found), [problems.ALERT_DELIVERY_FAILED, problems.TRACKER_STALLED])
        self.assertEqual(self.at(460, alerts={"gate": None}, check=False), {problems.TRACKER_STALLED: {}})


class EngineSavedPosition(unittest.TestCase):
    def test_the_restore_error_is_set_for_a_bad_file_only_and_cleared_by_the_next_save(self):
        import tempfile
        from _harness import make_engine
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "last.json")
            app = make_engine()
            app._last_aircraft_path = lambda: path
            load, save = type(app)._load_last_aircraft, type(app)._persist_last_aircraft
            self.assertIsNone(load(app))                                # no file: a new install
            self.assertIsNone(app._last_aircraft_restore_error)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{ not json")
            self.assertIsNone(load(app))
            self.assertIn("Error", app._last_aircraft_restore_error)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"attributes": {"latitude": 999, "longitude": 0}}')
            app._last_aircraft_restore_error = None
            self.assertIsNone(load(app))
            self.assertEqual(app._last_aircraft_restore_error, "the saved file holds no valid position")
            app._publish_feed_health(load_module_health(), 1000.0)
            self.assertEqual(app.states["sensor.vatsim_feed_health"][1]["last_aircraft_restore_error"],
                             "the saved file holds no valid position")
            app._last_aircraft_attrs = {"latitude": 10.0, "longitude": 20.0, "cid": "1234567"}
            save(app, force=True)
            self.assertIsNone(app._last_aircraft_restore_error)
            self.assertEqual(load(app)["latitude"], 10.0)


def load_module_health():
    return load("core.engine").FeedHealth.HEALTHY


class EngineNavdataExpiry(unittest.TestCase):
    def test_days_expired_count_from_the_last_valid_day_by_the_utc_date(self):
        from datetime import datetime, timedelta, timezone
        from _harness import make_engine
        engine = load("core.engine")
        app = make_engine()
        app.navdata_path = "/somewhere"
        today = datetime.now(timezone.utc).date()

        def loaded(valid_until):
            app._navdata = engine.NavDataSnapshot(
                version=1, fix_coords={"AAAAA": ((1.0, 2.0),)}, airport_coords={}, airway_graph={},
                loaded_at=1.0, load_issue_count=0, cycle="2001", valid_until=valid_until, source_mtime=1.0)
            return app._navdata_days_expired(), app._compute_navdata_status()
        self.assertEqual(loaded((today + timedelta(days=3)).isoformat()), (0, "active"))
        self.assertEqual(loaded(today.isoformat()), (0, "active"))            # the last day is still valid
        self.assertEqual(loaded((today - timedelta(days=1)).isoformat()), (1, "expired"))
        self.assertEqual(loaded((today - timedelta(days=9)).isoformat()), (9, "expired"))
        self.assertEqual(loaded(None), (0, "active"))
        self.assertEqual(loaded("not a date"), (0, "active"))
        app._publish_feed_health(engine.FeedHealth.HEALTHY, 1000.0)
        self.assertEqual(app.states["sensor.vatsim_feed_health"][1]["navdata_days_expired"], 0)


class EngineHeartbeat(unittest.TestCase):
    """R14 at its source: the engine's heartbeat moves only when a health check ran to its end."""

    def setUp(self):
        from _harness import make_engine
        self.engine_mod = load("core.engine")
        self.app = make_engine()

    def test_it_moves_on_a_check_even_when_nothing_changed_and_not_when_the_check_raises(self):
        app, health = self.app, self.engine_mod.FeedHealth.HEALTHY
        self.assertIsNone(app.last_health_check)
        app._publish_feed_health(health, 1000.0, timestamp_status="ok")
        first = app.last_health_check
        self.assertIsNotNone(first)
        writes = len(app.writes)
        app.last_health_check = None
        app._publish_feed_health(health, 1000.0, timestamp_status="ok")       # identical: not published again
        self.assertEqual(len(app.writes), writes)
        self.assertIsNotNone(app.last_health_check)
        app.last_health_check = None
        app._compute_navdata_status = lambda: 1 / 0                           # the health report itself fails
        with self.assertRaises(ZeroDivisionError):
            app._publish_feed_health(health, 2000.0, timestamp_status="ok")
        self.assertIsNone(app.last_health_check)


class EngineHealthWrites(unittest.TestCase):
    def setUp(self):
        from _harness import make_engine
        self.engine_mod = load("core.engine")
        self.app = make_engine()

    def test_a_health_write_that_failed_is_tried_again_and_is_not_a_finished_check(self):
        # R23: the health was remembered as published before the write, so with unchanged
        # health the write was never retried while the heartbeat kept moving.
        app, health = self.app, self.engine_mod.FeedHealth.DEGRADED
        real, attempts = app.set_state, []

        def failing(entity_id, **kwargs):
            attempts.append(entity_id)
            raise OSError("write refused")
        app.set_state = failing
        for _ in range(3):
            with self.assertRaises(OSError):
                app._publish_feed_health(health, 1000.0, timestamp_status="processing_error")
        self.assertEqual(len(attempts), 3)                    # every check tries again
        self.assertIsNone(app.last_health_check)              # and none of them counts
        app.set_state = real
        app._publish_feed_health(health, 1000.0, timestamp_status="processing_error")
        self.assertEqual(app.states["sensor.vatsim_feed_health"][0], health.value)
        self.assertIsNotNone(app.last_health_check)

    def test_a_navdata_load_that_works_clears_the_error_of_the_one_that_failed(self):
        # R17, the other half: the repair for a failed reload must be able to clear.
        app = self.app
        app._last_navdata_refresh_error, app._last_navdata_refresh_error_ts = "OSError: gone", 1.0
        snapshot = self.engine_mod.NavDataSnapshot(
            version=1, fix_coords={"AAAAA": ((1.0, 2.0),)}, airport_coords={}, airway_graph={},
            loaded_at=1.0, load_issue_count=0, cycle="2001", valid_until=None, source_mtime=1.0)
        app._apply_navdata_snapshot({"snapshot": snapshot})
        self.assertIsNone(app._last_navdata_refresh_error)


class Texts(unittest.TestCase):
    def test_every_problem_has_a_title_and_a_description_whose_placeholders_it_fills(self):
        with open(os.path.join(PACKAGE_DIR, "translations", "en.json"), encoding="utf-8") as fh:
            issues = json.load(fh)["issues"]
        self.assertEqual(sorted(issues), sorted(problems.ALL))
        # What the rules actually supply, taken from the rules: everything wrong at once.
        watch = problems.ProblemWatch(started=T0)
        wrong = {"last_success_ts": 0, "consecutive_failures": 3, "timestamp_status": "processing_error",
                 "geometry_status": "loading", "navdata_status": "configured_but_empty",
                 "metar_status": "error", "last_aircraft_restore_error": "ValueError: not JSON"}
        outside = {"gate": ("binary_sensor.example_hold", "unavailable"), "delivery_error": "push to x: failed"}
        for elapsed in (0, 3600):
            raised = watch.evaluate(T0 + elapsed, ("unavailable", wrong), True, T0 + elapsed, outside)
        raised.update(watch.evaluate(T0 + 3630, ("healthy", dict(HEALTHY, navdata_status="expired", navdata_days_expired=30)), True, T0 + 3630))
        stopped = problems.ProblemWatch(started=T0)
        for elapsed in (301, 331):
            raised.update(stopped.evaluate(T0 + elapsed, None, False, None))
        watch = problems.ProblemWatch(started=T0)
        watch.evaluate(T0, ("healthy", HEALTHY), True, T0)
        raised.update(watch.evaluate(T0 + 200, ("healthy", HEALTHY), True, T0 + 200))
        self.assertEqual(sorted(raised), sorted(problems.ALL))
        provided = {key: set(placeholders) for key, placeholders in raised.items()}
        for key in problems.ALL:
            self.assertTrue(issues[key]["title"] and issues[key]["description"], key)
            wanted = set(re.findall(r"\{(\w+)\}", issues[key]["title"] + issues[key]["description"]))
            self.assertEqual(wanted, provided.get(key, set()), key)
        self.assertTrue(problems.ERRORS <= set(problems.ALL))
        self.assertEqual(sorted(problems.PUSH_TITLES), sorted(problems.ALL))
        for key in problems.ALL:                       # a push says what the repair's title says
            self.assertEqual(problems.PUSH_TITLES[key], issues[key]["title"], key)


if __name__ == "__main__":
    unittest.main()
