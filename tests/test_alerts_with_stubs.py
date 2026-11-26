"""The alert sender, its entities and the options forms, driven through stand-ins (see
ha_stubs.py for what that does and does not prove). No engine: the tracker here is a plain
object holding what the engine would have published."""
import asyncio
import importlib
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import ha_stubs  # noqa: E402
from test_glue_with_stubs import DISPATCHER, _Hass  # noqa: E402  (also installs the stand-ins)

alerts_mod = importlib.import_module("custom_components.vatsim_atc.alerts")
const = importlib.import_module("custom_components.vatsim_atc.const")
flow_mod = importlib.import_module("custom_components.vatsim_atc.config_flow")
issues_mod = importlib.import_module("custom_components.vatsim_atc.issues")
problems = importlib.import_module("custom_components.vatsim_atc.core.problems")
rules = importlib.import_module("custom_components.vatsim_atc.core.alerts")

PHONE = "service:mobile_app_example_phone"
WATCH = "entity:notify.example_watch"
DESK, DEN = "media_player.example_desk", "media_player.example_den"
OPTIONS = {
    "notify_targets": [const.SIDEBAR_TARGET, PHONE, WATCH], "speakers": [DESK, DEN],
    "tts_entity": "tts.example_engine", "mute_gate_entity": "binary_sensor.example_hold",
}


class _Tracker:
    def __init__(self):
        self.published, self.last_health_check = {}, None


class Base(unittest.IsolatedAsyncioTestCase):
    options = OPTIONS

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = _Hass(asyncio.get_running_loop(), self.tmp.name)
        self.hass.states.set("binary_sensor.example_hold", "off")
        self.entry = ha_stubs.ConfigEntry("entry1", {"vatsim_cid": "1234567"}, dict(self.options))
        self.tracker = _Tracker()
        self.alerts = alerts_mod.AlertManager(self.hass, self.entry, self.tracker)
        self.tracker.alerts = self.alerts
        self.entry.runtime_data = self.tracker
        self.alerts.start()

    async def asyncTearDown(self):
        self.alerts.stop()
        DISPATCHER.targets.clear()
        self.tmp.cleanup()

    def calls(self, domain=None):
        return [c for c in self.hass.services.calls if domain is None or c[0] == domain]

    def publish(self, source, state, **attrs):
        self.tracker.published = {**self.tracker.published, source: (state, attrs)}
        DISPATCHER.send(self.hass, const.signal_for("entry1", source))


class Sending(Base):
    async def test_push_and_speech_go_where_the_options_say(self):
        await self.alerts.async_announce("Title", "Text.", rules.MODE_PUSH_AND_SPEAKER)
        self.assertEqual(self.calls("notify"), [
            ("notify", "persistent_notification", {"title": "Title", "message": "Text."}),
            ("notify", "mobile_app_example_phone", {"title": "Title", "message": "Text."}),
            ("notify", "send_message", {"entity_id": "notify.example_watch", "title": "Title", "message": "Text."})])
        self.assertEqual(self.calls("tts"), [
            ("tts", "speak", {"entity_id": "tts.example_engine", "media_player_entity_id": player, "message": "Text."})
            for player in (DESK, DEN)])
        self.assertEqual((self.alerts.push_error, self.alerts.speech_error), (None, None))

    async def test_each_mode_sends_only_what_it_names(self):
        expected = {rules.MODE_DISABLED: (0, 0), rules.MODE_PUSH_ONLY: (3, 0), rules.MODE_SPEAKER_ONLY: (0, 2),
                    rules.MODE_PUSH_AND_SPEAKER: (3, 2)}
        for mode, counts in expected.items():
            del self.hass.services.calls[:]
            await self.alerts.async_announce("T", "M", mode)
            self.assertEqual((len(self.calls("notify")), len(self.calls("tts"))), counts, mode)

    async def test_a_speaker_switched_off_is_skipped(self):
        self.alerts.speakers[DESK] = False
        await self.alerts.async_announce("T", "M", rules.MODE_SPEAKER_ONLY)
        self.assertEqual([c[2]["media_player_entity_id"] for c in self.calls("tts")], [DEN])

    async def test_speech_is_held_while_the_hold_entity_is_on_and_the_push_says_so(self):
        self.hass.states.set("binary_sensor.example_hold", "on")
        await self.alerts.async_announce("T", "M", rules.MODE_PUSH_AND_SPEAKER)
        self.assertEqual(self.calls("tts"), [])
        self.assertEqual(self.calls("notify")[0][2]["message"], "M (speakers held: binary_sensor.example_hold is on)")
        self.assertEqual(self.calls("persistent_notification"), [])           # held on purpose: no extra notice

    async def test_a_broken_hold_entity_holds_speech_and_says_so_on_screen(self):
        for state in ("unavailable", None):
            del self.hass.services.calls[:]
            if state is None:
                self.hass.states.all.pop("binary_sensor.example_hold")
            else:
                self.hass.states.set("binary_sensor.example_hold", state)
            await self.alerts.async_announce("T", "M", rules.MODE_SPEAKER_ONLY)
            self.assertEqual(self.calls("tts"), [])
            notice = self.calls("persistent_notification")
            self.assertEqual((len(notice), notice[0][2]["notification_id"]), (1, "vatsim_speech_held"), state)
            self.assertEqual(self.alerts.problem_inputs()["gate"],
                             ("binary_sensor.example_hold", state or "missing"))

    async def test_a_hold_that_starts_between_two_speakers_stops_the_second(self):
        original = self.hass.services.async_call

        async def call(domain, service, data=None, **kwargs):
            await original(domain, service, data, **kwargs)
            if domain == "tts":
                self.hass.states.set("binary_sensor.example_hold", "on")
        self.hass.services.async_call = call
        await self.alerts.async_announce("T", "M", rules.MODE_SPEAKER_ONLY)
        self.assertEqual([c[2]["media_player_entity_id"] for c in self.calls("tts")], [DESK])

    async def test_one_dead_speaker_does_not_stop_the_other_and_is_remembered(self):
        self.hass.services.failing[("tts", "speak")] = RuntimeError("speaker offline")
        await self.alerts.async_announce("T", "M", rules.MODE_PUSH_AND_SPEAKER)
        self.assertEqual(len(self.calls("tts")), 2)                           # both were tried
        self.assertEqual(len(self.calls("notify")), 3)                        # and the pushes went first
        self.assertIn("speech on media_player.example_desk: RuntimeError: speaker offline", self.alerts.speech_error)
        self.assertIsNone(self.alerts.push_error)
        self.assertIn("speech on", self.alerts.problem_inputs()["delivery_error"])
        del self.hass.services.failing[("tts", "speak")]
        await self.alerts.async_announce("T", "M", rules.MODE_PUSH_ONLY)      # a push working says nothing about speech
        self.assertIsNotNone(self.alerts.speech_error)
        await self.alerts.async_announce("T", "M", rules.MODE_SPEAKER_ONLY)
        self.assertIsNone(self.alerts.problem_inputs()["delivery_error"])

    async def test_a_failed_push_is_remembered_until_an_alert_push_works(self):
        self.hass.services.failing[("notify", "mobile_app_example_phone")] = KeyError("no such action")
        await self.alerts.async_announce("T", "M", rules.MODE_PUSH_ONLY)
        self.assertEqual(len(self.calls("notify")), 3)                        # the other two still went
        self.assertIn("push to mobile_app_example_phone: KeyError", self.alerts.push_error)
        del self.hass.services.failing[("notify", "mobile_app_example_phone")]
        await self.alerts.async_push("Problem", "Text", tag="vatsim_x", clears=False)
        self.assertIsNotNone(self.alerts.push_error)      # the push about the failure must not clear it
        await self.alerts.async_announce("T", "M", rules.MODE_PUSH_ONLY)
        self.assertIsNone(self.alerts.push_error)

    async def test_the_test_button_sends_both_whatever_the_mode_is(self):
        button = importlib.import_module("custom_components.vatsim_atc.button")
        found = []
        await button.async_setup_entry(self.hass, self.entry, found.extend)
        self.alerts.mode = rules.MODE_DISABLED
        self.alerts.enabled = {kind: False for kind in rules.KINDS}
        await found[0].async_press()
        self.assertEqual((len(self.calls("notify")), len(self.calls("tts"))), (3, 2))


class ProblemPushes(Base):
    async def test_a_new_repair_is_pushed_with_a_tag_whatever_the_alert_mode_is(self):
        self.alerts.mode = rules.MODE_DISABLED
        self.alerts.problem_raised(problems.FEED_STALE)
        await self.hass.settle()
        text = "Open Settings > System > Repairs for what to check."
        self.assertEqual(self.calls(), [
            ("notify", "mobile_app_example_phone", {"title": "VATSIM feed is not updating", "message": text,
                                                     "data": {"tag": "vatsim_feed_stale"}}),
            ("notify", "send_message", {"entity_id": "notify.example_watch",
                                        "title": "VATSIM feed is not updating", "message": text})])

    async def test_the_sidebar_notice_takes_a_tag_too_when_it_is_used(self):
        await self.alerts.async_push("T", "M", tag="vatsim_x", clears=True)
        self.assertEqual(self.calls()[0], ("notify", "persistent_notification", {
            "title": "T", "message": "M", "data": {"notification_id": "vatsim_x"}}))

    async def test_the_reporter_pushes_once_per_problem_and_reports_a_failed_delivery(self):
        reporter = issues_mod.IssueReporter(self.hass, self.entry, self.tracker, self.alerts.problem_inputs,
                                            self.alerts.problem_raised)
        self.tracker.last_health_check = issues_mod.time.monotonic()
        self.tracker.published = {"sensor.vatsim_feed_health": ("healthy", {
            "last_success_ts": 1.0, "geometry_status": "ready", "navdata_status": "expired", "navdata_days_expired": 30,
            "navdata_cycle": "2001", "navdata_valid_until": "2020-01-29"})}
        self.hass.services.failing[("notify", "mobile_app_example_phone")] = RuntimeError("phone gone")
        reporter.check()
        await self.hass.settle()
        self.assertEqual(list(self.hass.issues), [("vatsim_atc", "navdata_expired_entry1")])
        self.assertEqual(len(self.calls()), 2)                                # phone and watch, not the sidebar
        reporter.check()                                                     # the failed push is now a repair itself
        await self.hass.settle()
        failed = self.hass.issues[("vatsim_atc", "alert_delivery_failed_entry1")]
        self.assertIn("phone gone", failed["translation_placeholders"]["error"])
        pushes = len(self.calls())
        self.assertEqual(pushes, 4)                                           # one more attempt, for the new repair
        for _ in range(3):
            reporter.check()                                                 # nothing new: nothing more is sent
            await self.hass.settle()
        self.assertEqual(len(self.calls()), pushes)
        # The text of a repair changing is an update, not a new problem: no push for it.
        state, attrs = self.tracker.published["sensor.vatsim_feed_health"]
        self.tracker.published = {"sensor.vatsim_feed_health": (state, dict(attrs, navdata_cycle="2002"))}
        reporter.check()
        await self.hass.settle()
        self.assertEqual(self.hass.issues[("vatsim_atc", "navdata_expired_entry1")]["translation_placeholders"]["cycle"],
                         "2002")
        self.assertEqual(len(self.calls()), pushes)
        reporter.stop()

    async def test_pushes_for_repairs_can_be_switched_off(self):
        self.alerts.push_repairs = False
        self.alerts.problem_raised(problems.FEED_STALE)
        await self.hass.settle()
        self.assertEqual(self.calls(), [])


class FlightAlerts(Base):
    async def test_controller_online_waits_twenty_seconds_and_checks_again(self):
        self.publish(rules.COVERAGE, "on", last_change_reason="became_controlled", controller_callsign="ZZA_CTR")
        self.assertEqual([delay for delay, _ in self.hass.later], [20])
        self.assertEqual(self.calls(), [])
        self.hass.later.pop()[1](None)
        await self.hass.settle()
        self.assertEqual(self.calls("notify")[0][2],
                         {"title": "VATSIM ATC Online", "message": "Attention. ATC is now online: ZZA_CTR."})

    async def test_a_controller_who_left_within_the_wait_is_not_announced(self):
        self.publish(rules.COVERAGE, "on", last_change_reason="became_controlled", controller_callsign="ZZA_CTR")
        confirm = self.hass.later[0][1]
        self.publish(rules.COVERAGE, "off", last_change_reason="became_uncontrolled", controller_callsign=None)
        confirm(None)
        await self.hass.settle()
        self.assertEqual(self.calls(), [])

    async def test_next_controller_and_atis_change_are_sent_and_obey_their_switches(self):
        self.publish(rules.PILOT_CONNECTED, "on")
        self.publish(rules.NEXT_SECTOR, "different_controller", controller_callsign="ZZB_CTR")
        self.publish(rules.DESTINATION, "in_range", airport="ZZDD", atis_code="A", session="connected")
        self.publish(rules.DESTINATION, "in_range", airport="ZZDD", atis_code="B", session="connected")
        await self.hass.settle()
        self.assertEqual([c[2]["message"] for c in self.calls("notify")][::3], [
            "Heads up. Next likely controller: ZZB_CTR.", "ZZDD ATIS is now information Bravo."])
        del self.hass.services.calls[:]
        self.alerts.enabled[rules.NEXT_CONTROLLER] = self.alerts.enabled[rules.ATIS_CHANGE] = False
        self.publish(rules.NEXT_SECTOR, "different_controller", controller_callsign="ZZC_CTR")
        self.publish(rules.DESTINATION, "in_range", airport="ZZDD", atis_code="C", session="connected")
        await self.hass.settle()
        self.assertEqual(self.calls(), [])

    async def test_alert_mode_disabled_sends_nothing_and_stopping_ends_the_listening(self):
        self.alerts.mode = rules.MODE_DISABLED
        self.publish(rules.NEXT_SECTOR, "different_controller", controller_callsign="ZZB_CTR")
        await self.hass.settle()
        self.assertEqual(self.calls(), [])
        self.alerts.mode = rules.MODE_PUSH_ONLY
        self.publish(rules.COVERAGE, "on", last_change_reason="became_controlled", controller_callsign="ZZA_CTR")
        self.alerts.stop()
        self.assertEqual(self.hass.later, [])                                 # the wait is cancelled
        self.publish(rules.NEXT_SECTOR, "different_controller", controller_callsign="ZZC_CTR")
        await self.hass.settle()
        self.assertEqual(self.calls(), [])
        self.alerts.stop()                                                   # twice is harmless


class Controls(Base):
    async def add(self, platform):
        module = importlib.import_module(f"custom_components.vatsim_atc.{platform}")
        found = []
        await module.async_setup_entry(self.hass, self.entry, found.extend)
        for entity in found:
            entity.hass = self.hass
        return found

    async def test_the_mode_select_restores_and_sets_the_mode(self):
        select = (await self.add("select"))[0]
        self.assertEqual((select._attr_options, select._attr_current_option), (list(rules.MODES), rules.DEFAULT_MODE))
        select.last_state = ha_stubs.State("select.x", "speaker_only")
        await select.async_added_to_hass()
        self.assertEqual(self.alerts.mode, "speaker_only")
        select.last_state = ha_stubs.State("select.x", "unavailable")
        await select.async_added_to_hass()
        self.assertEqual(self.alerts.mode, "speaker_only")                    # not a mode: ignored
        await select.async_select_option("disabled")
        self.assertEqual((self.alerts.mode, select._attr_current_option), ("disabled", "disabled"))

    async def test_switches_for_each_alert_and_each_speaker(self):
        self.hass.states.set(DESK, "idle", name="Desk speaker")
        switches = await self.add("switch")
        self.assertEqual([s._attr_unique_id for s in switches], [
            "entry1_alert_controller_online", "entry1_alert_next_controller", "entry1_alert_atis_change",
            f"entry1_speaker_{DESK}", f"entry1_speaker_{DEN}"])
        self.assertEqual([getattr(s, "_attr_translation_placeholders", None) for s in switches[3:]],
                         [{"name": "Desk speaker"}, {"name": "example den"}])
        self.assertTrue(all(s.is_on for s in switches))                       # on until switched off
        await switches[1].async_turn_off()
        await switches[3].async_turn_off()
        self.assertEqual((self.alerts.enabled[rules.NEXT_CONTROLLER], self.alerts.speakers[DESK]), (False, False))
        await switches[3].async_turn_on()
        self.assertTrue(self.alerts.speakers[DESK])
        switches[4].last_state = ha_stubs.State("switch.x", "off")
        await switches[4].async_added_to_hass()
        self.assertFalse(self.alerts.speakers[DEN])

    async def test_the_switch_of_a_speaker_no_longer_chosen_is_removed(self):
        registry = self.hass.entity_registry
        for entity_id, unique in (("switch.old", "entry1_speaker_media_player.example_gone"),
                                  ("switch.desk", f"entry1_speaker_{DESK}"),
                                  ("switch.alert", "entry1_alert_atis_change"),
                                  ("sensor.health", "entry1_feed_health"),
                                  ("switch.other", "entry2_speaker_media_player.example_gone")):
            registry.entries[entity_id] = ha_stubs.RegistryEntry(entity_id, unique)
        await self.add("switch")
        self.assertEqual(registry.removed, ["switch.old"])


class Defaults(Base):
    options = {}

    async def test_a_new_install_pushes_to_the_sidebar_and_has_no_speakers(self):
        self.assertEqual((self.alerts.notify_targets, self.alerts.speaker_players, self.alerts.mode),
                         ([const.SIDEBAR_TARGET], [], rules.MODE_PUSH_ONLY))
        self.assertIsNone(self.alerts.gate())
        await self.alerts.async_test()
        self.assertEqual(self.calls(), [("notify", "persistent_notification", {
            "title": "VATSIM test alert", "message": "This is a test of the VATSIM alerts."})])
        self.alerts.problem_raised(problems.FEED_STALE)                       # the repair is already on screen
        await self.hass.settle()
        self.assertEqual(len(self.calls()), 1)


class OptionsForms(Base):
    def flow(self):
        flow = flow_mod.VatsimAtcOptionsFlow()
        flow.hass, flow.config_entry = self.hass, self.entry
        return flow

    async def test_configure_opens_a_menu_of_two_forms(self):
        self.assertEqual(await self.flow().async_step_init(),
                         {"type": "menu", "step_id": "init", "menu_options": ["sources", "alerts"]})

    async def test_each_form_keeps_the_other_forms_settings(self):
        self.entry.options = {**OPTIONS, "lookahead_minutes": 12.0, "navdata_path": self.tmp.name}
        saved = (await self.flow().async_step_sources({"lookahead_minutes": 9.0, "metar_fallback": False}))["data"]
        self.assertEqual(saved, {**OPTIONS, "lookahead_minutes": 9.0, "metar_fallback": False})   # folder cleared
        saved = (await self.flow().async_step_alerts({"notify_targets": [PHONE], "push_repairs": False}))["data"]
        self.assertEqual(saved, {"lookahead_minutes": 12.0, "navdata_path": self.tmp.name,
                                 "notify_targets": [PHONE], "speakers": [], "push_repairs": False})

    async def test_a_folder_home_assistant_cannot_see_is_refused(self):
        result = await self.flow().async_step_sources({"navdata_path": os.path.join(self.tmp.name, "absent")})
        self.assertEqual((result["type"], result["errors"]), ("form", {"navdata_path": "path_not_found"}))

    async def test_speakers_need_a_text_to_speech_engine(self):
        result = await self.flow().async_step_alerts({"speakers": [DESK]})
        self.assertEqual((result["type"], result["errors"]), ("form", {"tts_entity": "tts_needed"}))
        result = await self.flow().async_step_alerts({"speakers": [DESK], "tts_entity": "tts.example_engine"})
        self.assertEqual(result["data"]["tts_entity"], "tts.example_engine")

    async def test_no_push_targets_is_kept_as_none_not_as_never_chosen(self):
        saved = (await self.flow().async_step_alerts({"notify_targets": []}))["data"]
        self.assertEqual(saved["notify_targets"], [])
        self.entry.options = saved
        self.assertEqual(alerts_mod.AlertManager(self.hass, self.entry, self.tracker).notify_targets, [])

    async def test_the_push_list_offers_actions_and_entities_and_keeps_what_was_chosen(self):
        self.hass.services.notify = ["send_message", "notify", "persistent_notification", "mobile_app_example_phone"]
        self.hass.states.set("notify.example_watch", "unknown", name="Example watch")
        form = (await self.flow().async_step_alerts())["data_schema"]
        key = next(k for k in form if k == "notify_targets")
        self.assertEqual(key.default, [const.SIDEBAR_TARGET, PHONE, WATCH])
        offered = {o["value"]: o["label"] for o in form[key]["options"]}
        self.assertEqual(offered, {
            "service:mobile_app_example_phone": "notify.mobile_app_example_phone",
            "service:persistent_notification": "Sidebar notice",
            "entity:notify.example_watch": "Example watch (notify.example_watch)"})
        self.assertTrue(form[key]["multiple"])
        self.entry.options = {"notify_targets": ["service:mobile_app_old_phone"]}
        form = (await self.flow().async_step_alerts())["data_schema"]
        offered = {o["value"]: o["label"] for o in form[next(k for k in form if k == "notify_targets")]["options"]}
        self.assertEqual(offered["service:mobile_app_old_phone"], "mobile_app_old_phone (not found)")
        self.assertEqual({str(k): form[k].get("filter") for k in form if form[k].get("selector") == "entity"}, {
            "speakers": {"domain": "media_player"}, "tts_entity": {"domain": "tts"},
            "mute_gate_entity": {"domain": ["binary_sensor", "input_boolean", "switch"]}})


if __name__ == "__main__":
    unittest.main()
