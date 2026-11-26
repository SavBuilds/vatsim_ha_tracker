"""When the three flight alerts are due and what they say (core/alerts.py). No Home Assistant."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import PACKAGE_DIR, load  # noqa: E402

rules = load("core.alerts")


def coverage(state, reason, callsign=None):
    return (state, {"last_change_reason": reason, "controller_callsign": callsign})


def sector(state, callsign=None):
    return (state, {"controller_callsign": callsign})


def destination(state, airport, code, session="connected"):
    return (state, {"airport": airport, "atis_code": code, "session": session})


class ControllerOnline(unittest.TestCase):
    def test_it_starts_when_coverage_becomes_controlled(self):
        off = coverage("off", "became_uncontrolled")
        on = coverage("on", "became_controlled", "ZZA_CTR")
        self.assertTrue(rules.controller_online_started(off, on))
        self.assertTrue(rules.controller_online_started(None, on))          # connected under coverage
        self.assertFalse(rules.controller_online_started(on, on))
        self.assertFalse(rules.controller_online_started(on, coverage("on", "controller_changed", "ZZB_CTR")))
        self.assertFalse(rules.controller_online_started(off, coverage("on", "restored", "ZZA_CTR")))  # after a restart
        self.assertFalse(rules.controller_online_started(on, None))

    def test_it_is_announced_only_if_the_controller_is_still_there(self):
        self.assertEqual(rules.controller_online_message(coverage("on", "became_controlled", "ZZA_CTR")),
                         ("VATSIM ATC Online", "Attention. ATC is now online: ZZA_CTR."))
        self.assertIsNone(rules.controller_online_message(coverage("off", "became_uncontrolled", "ZZA_CTR")))
        for callsign in (None, "", "unknown"):
            self.assertIsNone(rules.controller_online_message(coverage("on", "became_controlled", callsign)))
        self.assertIsNone(rules.controller_online_message(None))
        self.assertEqual(rules.CONTROLLER_ONLINE_CONFIRM_SECONDS, 20)


class NextController(unittest.TestCase):
    def test_a_new_controller_ahead_is_announced_once(self):
        a, b = sector("different_controller", "ZZA_CTR"), sector("different_controller", "ZZB_CTR")
        self.assertEqual(rules.next_controller_message(sector("matches"), a),
                         ("VATSIM Next ATC", "Heads up. Next likely controller: ZZA_CTR."))
        self.assertEqual(rules.next_controller_message(a, b)[1], "Heads up. Next likely controller: ZZB_CTR.")
        self.assertIsNone(rules.next_controller_message(a, sector("different_controller", "ZZA_CTR")))
        self.assertIsNotNone(rules.next_controller_message(None, a))        # first publish of a session

    def test_states_that_name_no_controller_are_quiet(self):
        for state in ("unknown", "unavailable", "none", "matches", "skipped_low_speed",
                      "skipped_unstable_heading", "skipped_insufficient_history", None):
            self.assertIsNone(rules.next_controller_message(sector("x", "ZZA_CTR"), sector(state, "ZZB_CTR")), state)
        for callsign in (None, "", "unknown"):
            self.assertIsNone(rules.next_controller_message(sector("x"), sector("different_controller", callsign)))
        self.assertIsNone(rules.next_controller_message(sector("x", "ZZA_CTR"), None))


class AtisChange(unittest.TestCase):
    def test_a_new_letter_for_the_same_airport_is_announced_with_its_word(self):
        self.assertEqual(
            rules.atis_change_message(destination("in_range", "ZZDD", "A"), destination("in_range", "ZZDD", "b"), True),
            ("VATSIM ATIS", "ZZDD ATIS is now information Bravo."))
        self.assertEqual(rules.atis_change_message(destination("selected", "ZZDD", "A"),
                                                   destination("selected", "ZZDD", "7"), True)[1],
                         "ZZDD ATIS is now information 7.")

    def test_everything_else_is_quiet(self):
        old = destination("in_range", "ZZDD", "A")
        quiet = (
            (old, destination("in_range", "ZZDD", "A")),                       # same letter
            (destination("in_range", "ZZDD", None), destination("in_range", "ZZDD", "A")),   # first letter seen
            (old, destination("in_range", "ZZDD", "")),                        # ATIS gone
            (old, destination("in_range", "ZZEE", "B")),                       # another airport
            (destination("departure", "ZZDD", "A"), destination("in_range", "ZZDD", "B")),   # another phase
            (old, destination("out_of_range", "ZZDD", "B")),
            (old, destination("in_range", "ZZDD", "B", session="last_flight")),
            (None, destination("in_range", "ZZDD", "B")),
            (old, None),
        )
        for before, after in quiet:
            self.assertIsNone(rules.atis_change_message(before, after, True), (before, after))
        self.assertIsNone(rules.atis_change_message(old, destination("in_range", "ZZDD", "B"), False))  # not connected


class ModesAndTheHold(unittest.TestCase):
    def test_modes(self):
        self.assertEqual([m for m in rules.MODES if rules.push_enabled(m)], ["push_and_speaker", "push_only"])
        self.assertEqual([m for m in rules.MODES if rules.speech_enabled(m)], ["push_and_speaker", "speaker_only"])
        self.assertIn(rules.DEFAULT_MODE, rules.MODES)

    def test_speech_is_held_unless_the_entity_reads_off(self):
        self.assertFalse(rules.mute_gate_holds(None))                       # no entity set
        self.assertFalse(rules.mute_gate_holds("off"))
        for state in ("on", "unavailable", "unknown", "missing"):
            self.assertTrue(rules.mute_gate_holds(state), state)
        self.assertEqual([s for s in (None, "on", "off", "unavailable", "missing") if rules.mute_gate_broken(s)],
                         ["unavailable", "missing"])

    def test_the_mode_names_shown_to_the_user_cover_every_mode(self):
        with open(os.path.join(PACKAGE_DIR, "translations", "en.json"), encoding="utf-8") as fh:
            strings = json.load(fh)
        self.assertEqual(sorted(strings["entity"]["select"]["alert_mode"]["state"]), sorted(rules.MODES))
        self.assertEqual(sorted(k for k in strings["entity"]["switch"] if k != "speaker"),
                         sorted(f"alert_{kind}" for kind in rules.KINDS))


class Strings(unittest.TestCase):
    """The rules Hassfest applies to a translation file that this change could break
    (script/hassfest/translations.py, Home Assistant 2026.10.0). Not a replacement for Hassfest."""

    def test_translation_file(self):
        with open(os.path.join(PACKAGE_DIR, "translations", "en.json"), encoding="utf-8") as fh:
            strings = json.load(fh)
        slug = re.compile(r"^(?!.+[_-]{2})(?![_-])[a-z0-9-_]+(?<![_-])$")
        url = re.compile(r"(((ftp|ftps|scp|http|https|mqtt|mqtts|socket|socks5):\/\/|www\.)[a-z0-9]+)", re.I)

        def walk(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, path + (key,))
                return
            self.assertIsInstance(node, str, path)
            self.assertEqual(node, node.strip(), path)
            self.assertIsNone(re.search(r"<[a-z][^>]*>", node, re.I), path)       # no HTML
            self.assertIsNone(re.search(r"'{\w+}'", node), path)                   # no quoted placeholder
            self.assertIsNone(url.search(node), path)
            for name in re.findall(r"{([^}]*)}", node):
                self.assertTrue(name.isidentifier(), path)
        walk(strings, ())
        for flow in ("config", "options"):
            for step, info in strings[flow]["step"].items():
                for key in info.get("data_description", {}):
                    self.assertIn(key, info["data"], (flow, step))
        for platform, entities in strings["entity"].items():
            for key, info in entities.items():
                self.assertRegex(key, slug)
                for state, text in info.get("state", {}).items():
                    self.assertRegex(state, slug)
                    self.assertNotIn("{", text)                                 # no placeholders in states
        self.assertEqual(sorted(strings["options"]["step"]["init"]["menu_options"]), ["alerts", "sources"])


if __name__ == "__main__":
    unittest.main()
