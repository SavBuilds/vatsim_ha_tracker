"""The Home Assistant side of the integration, driven through stand-ins (see ha_stubs.py for
what that does and does not prove). The engine, worker threads and timers underneath are real."""
import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
import ha_stubs  # noqa: E402
import scenario as a  # noqa: E402

DISPATCHER = ha_stubs.install()
integration = importlib.import_module("custom_components.vatsim_atc")
engine = importlib.import_module("custom_components.vatsim_atc.core.engine")
const = importlib.import_module("custom_components.vatsim_atc.const")


class _Response:
    def __init__(self, text):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def text(self):
        return self._text


class _Session:
    def __init__(self):
        self.headers_seen = []

    def get(self, url, headers=None, timeout=None):
        self.headers_seen.append(headers)
        return _Response(a.serve(url, a.feed(23.0)))


class _Hass:
    def __init__(self, loop, folder):
        self.loop, self.session, self.entities, self.stop_listeners = loop, _Session(), [], []
        self.issues, self.issue_events, self.intervals, self.data = {}, [], [], {}
        self.later, self.tasks = [], []
        self.states, self.entity_registry = ha_stubs.States(), ha_stubs.EntityRegistry()
        outer = self
        self.config = type("C", (), {"path": staticmethod(lambda *p: os.path.join(folder, *p))})()
        self.bus = type("B", (), {"async_listen_once": staticmethod(
            lambda event, cb: outer.stop_listeners.append((event, cb)) or (lambda: None))})()
        self.services = ha_stubs.Services()

        class Entries:
            async def async_forward_entry_setups(self, entry, platforms):
                for platform in platforms:
                    module = importlib.import_module(f"custom_components.vatsim_atc.{platform.value}")
                    found = []
                    await module.async_setup_entry(outer, entry, lambda ents: found.extend(ents))
                    for entity in found:
                        entity.hass = outer
                        entity.platform_name = platform.value
                        await entity.async_added_to_hass()
                    outer.entities.extend(found)

            async def async_unload_platforms(self, entry, platforms):
                return True
        self.config_entries = Entries()

    def async_create_task(self, coro, name=None, eager_start=True):
        task = self.loop.create_task(coro)
        self.tasks.append(task)
        return task

    async def settle(self):
        """Wait for everything the integration started in the background."""
        while self.tasks:
            await self.tasks.pop(0)

    async def async_add_executor_job(self, func, *args):
        return await self.loop.run_in_executor(None, func, *args)


async def until(predicate, seconds=15.0):
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


class Glue(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, ".storage"))
        self.hass = _Hass(asyncio.get_running_loop(), self.tmp.name)
        self._jitter = engine.POLL_JITTER_SECONDS
        engine.POLL_JITTER_SECONDS = 0.0
        # The CID is the only thing a user enters; the rest stands in for the public data sources.
        self._args = const.engine_args
        const_args = lambda data, options: {**self._args(data, options), "vatspy_boundaries_url": a.BOUNDARIES_URL,  # noqa: E731
                                            "vatspy_dat_url": a.DAT_URL, "poll_interval_seconds": 0.15}
        integration.engine_args = const_args
        self.entry = ha_stubs.ConfigEntry("entry1", {"vatsim_cid": "1234567"}, {"metar_fallback": False})
        self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        self.by_key = {e._attr_translation_key: e for e in self.hass.entities}

    async def asyncTearDown(self):
        await integration.async_unload_entry(self.hass, self.entry)
        DISPATCHER.targets.clear()   # the stand-in platform unload does not disconnect entities
        engine.POLL_JITTER_SECONDS = self._jitter
        integration.engine_args = self._args
        self.tmp.cleanup()

    async def test_entities_show_what_the_engine_publishes(self):
        e = self.by_key
        self.assertEqual(len(self.hass.entities), 15)   # 10 that show the engine, 5 for the alerts
        self.assertFalse(e["next_sector"].available)   # nothing published yet
        self.assertTrue(await until(lambda: e["next_sector"].extra_state_attributes.get("next_controller_callsign")))
        self.assertEqual(e["feed_health"].native_value, "healthy")
        self.assertIs(e["pilot_connected"].is_on, True)
        self.assertIs(e["atc_coverage"].is_on, True)
        self.assertEqual(e["atc_coverage"].extra_state_attributes["controller_callsign"], "ZZA_CTR")
        self.assertEqual((e["aircraft"].latitude, e["aircraft"].longitude), (10.0, 23.0))
        self.assertEqual(e["next_sector"].extra_state_attributes["eta_text"], "7 min")
        for entity in self.hass.entities:
            if not hasattr(entity, "_source_id"):
                continue                               # the text box and the alert controls
            attrs = entity.extra_state_attributes
            self.assertFalse({"pub_token", "friendly_name"} & set(attrs), entity._attr_translation_key)
            self.assertGreater(entity.__dict__.get("writes", 0), 0, entity._attr_translation_key)
        self.assertFalse({"latitude", "longitude"} & set(e["aircraft"].extra_state_attributes))
        self.assertIn("callsign", e["aircraft"].extra_state_attributes)

    async def test_unique_ids_and_names(self):
        ids = [e._attr_unique_id for e in self.hass.entities]
        self.assertEqual(len(ids), len(set(ids)))
        with open(os.path.join(ROOT, "custom_components", "vatsim_atc", "translations", "en.json"), encoding="utf-8") as fh:
            names = json.load(fh)["entity"]
        for entity in self.hass.entities:
            self.assertIn(entity._attr_translation_key, names[entity.platform_name])
        # One name per entity, plus the name shared by the per-speaker switches (none set up here).
        self.assertEqual(sum(len(v) for v in names.values()), len(self.hass.entities) + 1)

    async def test_typed_airport_drives_the_destination(self):
        e = self.by_key
        self.assertTrue(await until(lambda: e["destination"].available))
        await e["atis_airport"].async_set_value(" zzdd ")
        self.assertEqual(e["atis_airport"]._attr_native_value, "ZZDD")
        self.assertTrue(await until(lambda: e["destination"].native_value == "selected"))

    async def test_user_agent_and_save_file(self):
        self.assertTrue(await until(lambda: self.by_key["pilot_connected"].is_on))
        self.assertTrue(all(h and "vatsim_ha_tracker/" in h["User-Agent"] for h in self.hass.session.headers_seen))
        self.assertEqual(self.hass.stop_listeners[0][0], "homeassistant_stop")
        await self.hass.stop_listeners[0][1](None)      # Home Assistant stopping
        saved = os.path.join(self.tmp.name, ".storage", "vatsim_atc.entry1.last_aircraft.json")
        self.assertTrue(os.path.exists(saved))
        await integration.async_remove_entry(self.hass, self.entry)
        self.assertFalse(os.path.exists(saved))

    async def test_problems_become_repairs_issues_and_clear_again(self):
        issues_mod = importlib.import_module("custom_components.vatsim_atc.issues")
        problems = importlib.import_module("custom_components.vatsim_atc.core.problems")
        severity = sys.modules["homeassistant.helpers.issue_registry"].IssueSeverity
        self.assertEqual(len(self.hass.intervals), 1)                     # the reporter's timer is running
        check, interval = self.hass.intervals[0]
        self.assertEqual(interval.total_seconds(), 30)
        tracker = self.entry.runtime_data
        self.assertTrue(await until(lambda: tracker.published.get("sensor.vatsim_feed_health", (None,))[0] == "healthy"))
        self.assertIsNotNone(tracker.last_health_check)                   # the engine's own heartbeat
        check()
        self.assertEqual(self.hass.issues, {})                            # healthy: nothing raised

        # The reporter must read the monotonic clock only. The wall clock is moved an hour
        # each way below and nothing may come of it.
        real_time, clock = issues_mod.time, type("Clock", (), {"offset": 0.0, "wall": 0.0})()
        clock.monotonic = lambda: real_time.monotonic() + clock.offset
        clock.time = lambda: real_time.time() + clock.wall
        issues_mod.time = clock
        try:
            for clock.wall in (3600.0, -3600.0):
                check()
                self.assertEqual(self.hass.issues, {})
            # Boundaries lost, and navigation data expired, as the engine would publish them.
            state, attrs = tracker.published["sensor.vatsim_feed_health"]
            broken = dict(attrs, geometry_status="loading", last_geometry_refresh_error="OSError: no route",
                          navdata_status="expired", navdata_days_expired=30, navdata_cycle="2001", navdata_valid_until="2020-01-29")
            freeze = tracker.set_state
            tracker.set_state = lambda *a_, **k: None                     # hold the engine's own publishes off
            tracker.published = {**tracker.published, "sensor.vatsim_feed_health": (state, broken)}
            check()
            self.assertEqual(list(self.hass.issues), [("vatsim_atc", "navdata_expired_entry1")])   # no hold on this one
            expired = self.hass.issues[("vatsim_atc", "navdata_expired_entry1")]
            self.assertEqual(expired["translation_key"], "navdata_expired")
            self.assertEqual(expired["translation_placeholders"], {"cycle": "2001", "valid_until": "2020-01-29"})
            self.assertEqual((expired["is_fixable"], expired["is_persistent"], expired["severity"]),
                             (False, False, severity.WARNING))
            clock.offset = problems.HOLD_BOUNDARIES + 1
            tracker.last_health_check = clock.monotonic()
            broken["last_success_ts"] = broken["last_success_ts"] + 1      # a newer good download
            check()
            boundaries = self.hass.issues[("vatsim_atc", "boundaries_not_loaded_entry1")]
            self.assertEqual(boundaries["translation_placeholders"], {"error": "OSError: no route"})
            # The engine goes quiet: that is reported as an error, and what was already
            # reported stays, with no second notice for it.
            del self.hass.issue_events[:]
            clock.offset += problems.STALLED_SECONDS + 1
            check()                                                       # one quiet reading changes nothing
            self.assertEqual(self.hass.issue_events, [])
            clock.offset += 30
            check()
            self.assertEqual(sorted(k for _, k in self.hass.issues), [
                "boundaries_not_loaded_entry1", "navdata_expired_entry1", "tracker_stalled_entry1"])
            stalled = self.hass.issues[("vatsim_atc", "tracker_stalled_entry1")]
            self.assertEqual((stalled["severity"], stalled["translation_placeholders"]), (severity.ERROR, None))
            tracker.last_health_check = clock.monotonic()                 # and comes back
            broken["last_success_ts"] = broken["last_success_ts"] + 1      # with a fresh download
            check()
            self.assertEqual(self.hass.issue_events, [("create", "tracker_stalled_entry1"),
                                                      ("remove", "tracker_stalled_entry1")])
            # Unloading clears whatever is raised and stops the timer.
            for stop in self.entry.unload_callbacks:
                stop()
            self.assertEqual(self.hass.issues, {})
            self.assertEqual(self.hass.intervals, [])
            self.assertEqual(self.hass.data["vatsim_atc"], {})
            tracker.set_state = freeze
        finally:
            issues_mod.time = real_time

    async def test_home_assistant_stopping_ends_the_checks_without_reporting_a_stopped_tracker(self):
        self.assertEqual(len(self.hass.intervals), 1)
        await self.hass.stop_listeners[0][1](None)
        self.assertEqual(self.hass.intervals, [])
        self.assertEqual(self.hass.issues, {})

    async def test_removing_an_entry_whose_unload_failed_still_stops_everything(self):
        tracker = self.entry.runtime_data
        self.assertTrue(await until(lambda: tracker.published.get("sensor.vatsim_feed_health", (None,))[0] == "healthy"))
        reporter = self.hass.data["vatsim_atc"]["entry1"]
        state, attrs = tracker.published["sensor.vatsim_feed_health"]
        tracker.published = {**tracker.published, "sensor.vatsim_feed_health": (
            state, dict(attrs, navdata_status="expired", navdata_days_expired=30, navdata_cycle="2001", navdata_valid_until="2020-01-29"))}
        reporter.check()
        self.assertEqual(len(self.hass.issues), 1)
        # No unload callback has run: this is what Home Assistant does after a failed unload.
        await integration.async_remove_entry(self.hass, self.entry)
        self.assertEqual((self.hass.issues, self.hass.intervals, self.hass.data["vatsim_atc"]), ({}, [], {}))
        self.assertTrue(tracker._stopped.is_set())
        await integration.async_remove_entry(self.hass, self.entry)       # a second call is harmless

    async def test_the_issue_stand_in_refuses_what_home_assistant_would_refuse(self):
        registry = sys.modules["homeassistant.helpers.issue_registry"]
        with self.assertRaises(TypeError):
            registry.async_create_issue(self.hass, "vatsim_atc", "x", typo_argument=True)
        with self.assertRaises(TypeError):
            registry.async_create_issue(self.hass, "vatsim_atc", "x", is_fixable=False, translation_key="k")

    async def test_restored_airport_is_sanitised(self):
        text = importlib.import_module("custom_components.vatsim_atc.text")
        box = text.AtisAirportText(self.entry, self.entry.runtime_data)
        box.hass = self.hass
        box.last_state = type("S", (), {"state": "not an airport"})()
        await box.async_added_to_hass()
        self.assertEqual(box._attr_native_value, "")
        box.last_state = type("S", (), {"state": "kbos"})()
        await box.async_added_to_hass()
        self.assertEqual(box._attr_native_value, "KBOS")


class BadConfig(unittest.IsolatedAsyncioTestCase):
    async def test_placeholder_cid_fails_setup_cleanly(self):
        with tempfile.TemporaryDirectory() as folder:
            hass = _Hass(asyncio.get_running_loop(), folder)
            entry = ha_stubs.ConfigEntry("entry2", {"vatsim_cid": "0"})
            with self.assertRaises(sys.modules["homeassistant.exceptions"].ConfigEntryError):
                await integration.async_setup_entry(hass, entry)
            self.assertEqual(hass.entities, [])


if __name__ == "__main__":
    unittest.main()
