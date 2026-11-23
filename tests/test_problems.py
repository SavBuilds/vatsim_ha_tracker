"""The rules that decide which problems are reported (core/problems.py). No Home Assistant."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import PACKAGE_DIR, load  # noqa: E402

problems = load("core.problems")
T0 = 1_000_000.0
HEALTHY = {"last_success_ts": T0, "consecutive_failures": 0, "timestamp_status": "ok",
           "geometry_status": "ready", "navdata_status": "active", "navdata_load_issue_count": 0}


class Rules(unittest.TestCase):
    def setUp(self):
        self.watch = problems.ProblemWatch(started=T0)

    def run_for(self, seconds, attrs, connected=False, step=30, fresh=True, start=0):
        """Evaluate every `step` seconds, as the integration's timer does. fresh=True keeps the
        last good download current, the way a working feed would."""
        active = {}
        for elapsed in range(start, start + seconds + 1, step):
            now = T0 + elapsed
            current = dict(attrs)
            if fresh and current.get("last_success_ts"):
                current["last_success_ts"] = now
            active = self.watch.evaluate(now, ("healthy", current), connected, now)
        return active

    def test_a_healthy_tracker_reports_nothing(self):
        self.assertEqual(self.run_for(3600, HEALTHY), {})
        self.assertEqual(self.run_for(3600, HEALTHY, connected=True, start=3600), {})

    def test_nothing_is_reported_before_the_first_publish_until_the_grace_time_is_up(self):
        self.assertEqual(self.watch.evaluate(T0 + 299, None, False, None), {})
        self.assertEqual(self.watch.evaluate(T0 + 301, None, False, None), {problems.TRACKER_STALLED: {}})

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
            watch = problems.ProblemWatch(started=T0)
            before = watch.evaluate(T0 + limit - 1, ("unavailable", old), connected, T0 + limit - 1)
            after = watch.evaluate(T0 + limit + 1, ("unavailable", old), connected, T0 + limit + 1)
            self.assertEqual((before, list(after)), ({}, [problems.FEED_STALE]), connected)
        self.assertLess(problems.STALE_CONNECTED_SECONDS, problems.STALE_IDLE_SECONDS)

    def test_a_stopped_engine_is_the_only_thing_reported(self):
        broken = dict(HEALTHY, geometry_status="loading", navdata_status="expired")
        now = T0 + 5000
        self.assertEqual(self.watch.evaluate(now, ("healthy", broken), True, now - problems.STALLED_SECONDS - 1),
                         {problems.TRACKER_STALLED: {}})
        self.assertNotIn(problems.TRACKER_STALLED, self.watch.evaluate(now, ("healthy", broken), True, now - 10))

    def test_processing_error_boundaries_and_navdata_each_need_five_minutes(self):
        cases = ((problems.PROCESSING_ERROR, dict(HEALTHY, timestamp_status="processing_error")),
                 (problems.BOUNDARIES_NOT_LOADED, dict(HEALTHY, geometry_status="loading")),
                 (problems.NAVDATA_NOT_LOADED, dict(HEALTHY, navdata_status="configured_but_empty")),
                 (problems.NAVDATA_NOT_LOADED, dict(HEALTHY, navdata_status="configured_but_unloaded")),
                 (problems.NAVDATA_NOT_LOADED, dict(HEALTHY, navdata_load_issue_count=2)))
        for key, attrs in cases:
            self.watch = problems.ProblemWatch(started=T0)
            self.assertEqual(self.run_for(270, attrs), {}, key)
            self.assertEqual(list(self.run_for(30, attrs, start=300)), [key], attrs)
            self.assertEqual(self.run_for(30, HEALTHY, start=360), {}, key)

    def test_expired_navdata_is_reported_at_once_with_its_cycle(self):
        expired = dict(HEALTHY, navdata_status="expired", navdata_cycle="2001", navdata_valid_until="2020-01-29")
        self.assertEqual(self.run_for(0, expired),
                         {problems.NAVDATA_EXPIRED: {"cycle": "2001", "valid_until": "2020-01-29"}})

    def test_disabled_navdata_and_a_missing_geometry_status_are_not_problems(self):
        self.assertEqual(self.run_for(3600, dict(HEALTHY, navdata_status="disabled")), {})
        without = {k: v for k, v in HEALTHY.items() if k != "geometry_status"}
        self.assertEqual(self.run_for(3600, without), {})

    def test_error_text_is_carried_and_bounded(self):
        attrs = dict(HEALTHY, geometry_status="loading", last_geometry_refresh_error="E" * 5000)
        text = self.run_for(330, attrs)[problems.BOUNDARIES_NOT_LOADED]["error"]
        self.assertEqual(len(text), 300)
        self.watch = problems.ProblemWatch(started=T0)
        none = self.run_for(330, dict(HEALTHY, geometry_status="loading"))
        self.assertEqual(none[problems.BOUNDARIES_NOT_LOADED]["error"], "no error was recorded")

    def test_placeholders_are_strings_and_stable_between_checks(self):
        attrs = dict(HEALTHY, geometry_status="loading", navdata_status="expired", navdata_cycle=2001,
                     consecutive_failures=1)
        first = self.run_for(330, attrs)
        attrs["consecutive_failures"] = 99                    # a counter that moves every poll
        second = self.run_for(30, attrs, start=360)
        self.assertEqual(first, second)
        for placeholders in first.values():
            self.assertTrue(all(isinstance(v, str) for v in placeholders.values()))


class Texts(unittest.TestCase):
    def test_every_problem_has_a_title_and_a_description_whose_placeholders_it_fills(self):
        with open(os.path.join(PACKAGE_DIR, "translations", "en.json"), encoding="utf-8") as fh:
            issues = json.load(fh)["issues"]
        self.assertEqual(sorted(issues), sorted(problems.ALL))
        provided = {problems.BOUNDARIES_NOT_LOADED: {"error"}, problems.NAVDATA_NOT_LOADED: {"status", "error"},
                    problems.NAVDATA_EXPIRED: {"cycle", "valid_until"}}
        for key in problems.ALL:
            self.assertTrue(issues[key]["title"] and issues[key]["description"], key)
            wanted = set(re.findall(r"\{(\w+)\}", issues[key]["title"] + issues[key]["description"]))
            self.assertEqual(wanted, provided.get(key, set()), key)
        self.assertTrue(problems.ERRORS <= set(problems.ALL))


if __name__ == "__main__":
    unittest.main()
