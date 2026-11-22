"""The event-loop host with the real engine on it: a real asyncio loop on its own thread,
real worker and loader threads, and a fake network. No Home Assistant."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scenario as a  # noqa: E402
from _harness import load  # noqa: E402

host_mod, engine_mod, const = load("host"), load("core.engine"), load("const")


class LoopThread:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def close(self):
        async def settle():
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(settle(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def wait_for(predicate, seconds=15.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class HostWithEngine(unittest.TestCase):
    def setUp(self):
        self.lt = LoopThread()
        self.tmp = tempfile.TemporaryDirectory()
        self.published_on = []      # (entity id, thread name) for every publish notification
        self.fetch_threads = set()
        self.fail = None
        self.payload = a.feed(23.0)
        self._jitter = engine_mod.POLL_JITTER_SECONDS
        engine_mod.POLL_JITTER_SECONDS = 0.0
        self.tracker = self.make()

    def tearDown(self):
        self.tracker.shutdown()
        engine_mod.POLL_JITTER_SECONDS = self._jitter
        self.lt.close()
        self.tmp.cleanup()

    def make(self, **extra):
        args = const.engine_args({"vatsim_cid": "1234567"}, {"metar_fallback": False})
        args.update(vatspy_boundaries_url=a.BOUNDARIES_URL, vatspy_dat_url=a.DAT_URL, poll_interval_seconds=0.15)
        args.update(extra)
        return host_mod.Tracker(args, self.lt.loop, self.fetch, self.on_publish,
                                last_aircraft_file=os.path.join(self.tmp.name, "last.json"))

    async def fetch(self, url, timeout, headers):
        self.fetch_threads.add(threading.current_thread().name)
        if self.fail is not None:
            raise self.fail
        return a.serve(url, a.feed(23.0))   # fresh timestamp each poll

    def on_publish(self, entity_id):
        self.published_on.append((entity_id, threading.current_thread().name))

    def state(self, entity_id):
        return self.tracker.published.get(entity_id, (None, {}))

    def test_runs_end_to_end(self):
        self.tracker.start()
        self.assertTrue(wait_for(lambda: self.state("sensor.vatsim_next_sector")[1].get("next_controller_callsign")),
                        self.tracker.published.keys())
        state, attrs = self.state("binary_sensor.vatsim_atc_coverage")
        self.assertEqual((state, attrs["controller_callsign"]), ("on", "ZZA_CTR"))
        self.assertEqual(self.state("sensor.vatsim_next_sector")[1]["eta_text"], "7 min")
        self.assertEqual(self.state("binary_sensor.vatsim_pilot_connected")[0], "on")
        self.assertEqual(len(self.tracker.published), 9)

    def test_engine_never_runs_on_the_loop_and_notifications_always_do(self):
        loop_thread = self.lt.thread.name
        seen = set()
        original = self.tracker._run_poll_cycle_body
        self.tracker._run_poll_cycle_body = lambda: (seen.add(threading.current_thread().name), original())[1]
        self.tracker.start()
        self.assertTrue(wait_for(lambda: len(seen) >= 1 and len(self.published_on) >= 5))
        self.assertTrue(wait_for(lambda: self.state("binary_sensor.vatsim_atc_coverage")[0] == "on"))
        self.assertEqual({name for _e, name in self.published_on}, {loop_thread})
        self.assertEqual(self.fetch_threads, {loop_thread})
        self.assertNotIn(loop_thread, seen)
        self.assertEqual(len(seen), 1, seen)   # every poll on the one worker thread

    def test_none_attributes_are_dropped_and_plumbing_is_kept_for_the_entity_layer(self):
        self.tracker.start()
        self.assertTrue(wait_for(lambda: "sensor.vatsim_feed_health" in self.tracker.published))
        for entity_id, (_state, attrs) in self.tracker.published.items():
            stable = const.STABLE_ATTRIBUTES.get(entity_id, ())
            self.assertEqual([k for k, v in attrs.items() if v is None and k not in stable], [], entity_id)

    def test_attributes_the_alerts_read_are_present_even_with_no_one_connected(self):
        self.tracker = self.make(vatsim_cid="7654321")     # a CID that is not in the feed
        self.tracker.start()
        self.assertTrue(wait_for(lambda: all(e in self.tracker.published for e in const.STABLE_ATTRIBUTES)),
                        self.tracker.published.keys())
        self.assertEqual(self.state("binary_sensor.vatsim_pilot_connected")[0], "off")
        for entity_id, keys in const.STABLE_ATTRIBUTES.items():
            for key in keys:
                self.assertIn(key, self.state(entity_id)[1], entity_id)
                self.assertIsNone(self.state(entity_id)[1][key], entity_id)

    def test_a_stable_attribute_keeps_its_value_when_it_has_one(self):
        self.tracker.start()
        self.assertTrue(wait_for(lambda: self.state("binary_sensor.vatsim_atc_coverage")[0] == "on"))
        self.assertEqual(self.state("binary_sensor.vatsim_atc_coverage")[1]["controller_callsign"], "ZZA_CTR")

    def test_feed_failure_becomes_unavailable_not_a_crash(self):
        self.tracker.start()
        self.assertTrue(wait_for(lambda: self.state("sensor.vatsim_feed_health")[0] == "healthy"))
        self.fail = OSError("network unreachable")
        self.assertTrue(wait_for(lambda: self.state("sensor.vatsim_feed_health")[0] == "unavailable"))
        self.fail = None
        self.assertTrue(wait_for(lambda: self.state("sensor.vatsim_feed_health")[0] == "healthy"))

    def test_fetch_timeout_is_a_read_timeout(self):
        async def never(url, timeout, headers):
            await asyncio.sleep(60)
        tracker = host_mod.Tracker({"vatsim_cid": "1234567"}, self.lt.loop, never, lambda e: None,
                                   last_aircraft_file=os.path.join(self.tmp.name, "x.json"))
        old = host_mod.FETCH_GRACE_SECONDS
        host_mod.FETCH_GRACE_SECONDS = 0.0
        try:
            with self.assertRaises(engine_mod.FetchError) as ctx:
                tracker.http_get("https://example.invalid/", timeout=0.2)
        finally:
            host_mod.FETCH_GRACE_SECONDS = old
            tracker.shutdown()
        self.assertTrue(ctx.exception.is_timeout)
        self.assertEqual(ctx.exception.kind, "ReadTimeout")

    def test_http_get_on_the_loop_is_refused(self):
        result = {}

        async def call():
            try:
                self.tracker.http_get("https://example.invalid/", timeout=1)
            except RuntimeError as exc:
                result["error"] = str(exc)
        asyncio.run_coroutine_threadsafe(call(), self.lt.loop).result(5)
        self.assertIn("event loop", result.get("error", ""))

    def test_typed_airport_reaches_the_engine(self):
        self.tracker.start()
        self.assertTrue(wait_for(lambda: "sensor.vatsim_destination" in self.tracker.published))
        self.tracker.set_local_state(engine_mod.ATIS_AIRPORT_OVERRIDE_ENTITY_ID, "zzdd")
        self.assertTrue(wait_for(lambda: self.state("sensor.vatsim_destination")[0] == "selected"),
                        self.state("sensor.vatsim_destination"))
        self.tracker.set_local_state(engine_mod.ATIS_AIRPORT_OVERRIDE_ENTITY_ID, "")
        self.assertTrue(wait_for(lambda: self.state("sensor.vatsim_destination")[0] != "selected"))

    def test_shutdown_stops_polling_and_saves_the_aircraft(self):
        self.tracker.start()
        self.assertTrue(wait_for(lambda: self.state("binary_sensor.vatsim_pilot_connected")[0] == "on"))
        self.tracker.shutdown()
        count = len(self.published_on)
        time.sleep(0.6)
        self.assertEqual(len(self.published_on), count)
        with open(os.path.join(self.tmp.name, "last.json"), encoding="utf-8") as fh:
            self.assertIn("latitude", fh.read())
        self.tracker = self.make()   # so tearDown has something to shut down

    def test_bad_cid_is_refused_before_start(self):
        tracker = host_mod.Tracker({"vatsim_cid": "replace_me"}, self.lt.loop, self.fetch, self.on_publish,
                                   last_aircraft_file=os.path.join(self.tmp.name, "y.json"))
        with self.assertRaises(ValueError):
            tracker.check_config()
        tracker.shutdown()


class EngineArgs(unittest.TestCase):
    def test_blank_options_are_left_to_engine_defaults(self):
        args = const.engine_args({"vatsim_cid": " 1234567 "},
                                 {"navdata_path": "  ", "simaware_boundaries_path": None,
                                  "lookahead_minutes": 12, "metar_fallback": False})
        self.assertEqual(args, {"vatsim_cid": "1234567", "lookahead_minutes": 12, "metar_fallback": False})


if __name__ == "__main__":
    unittest.main()
