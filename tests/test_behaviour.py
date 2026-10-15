"""Run with python -m unittest discover -s tests -p test_behaviour.py -v.

All desktop I/O and elapsed time are injected; these tests NEVER type into apps.
"""
from datetime import datetime, timezone
import inspect
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest

from behaviour.interaction import BehaviourEngine
from behaviour.timing import DEFAULT_PROFILES, PauseProfile, TimingModel, summarise_intervals
from behaviour.typing import CancelledError, TypingSettings, WindowsKeyboard


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, value):
        self.sleeps.append(value)
        self.now += value


class FakeKeyboard:
    def __init__(self):
        self.text = ""
        self.calls = []

    def write(self, text):
        self.calls.append(("write", text))
        self.text += text

    def press(self, key):
        self.calls.append(("press", key))
        if key == "backspace":
            self.text = self.text[:-1]
        elif key == "enter":
            self.text += "\n"
        elif key == "tab":
            self.text += "\t"
        else:
            raise AssertionError(key)


def make_engine(**kwargs):
    clock, keyboard = FakeClock(), FakeKeyboard()
    engine = BehaviourEngine(keyboard=keyboard, sleeper=clock.sleep, clock=clock,
        wall_clock=lambda: datetime(2026, 10, 11, tzinfo=timezone.utc), **kwargs)
    return engine, clock, keyboard


class BehaviourTests(unittest.TestCase):
    def test_contract_signatures(self):
        self.assertEqual(list(inspect.signature(BehaviourEngine.pause).parameters), ["self", "action"])
        self.assertEqual(list(inspect.signature(BehaviourEngine.type_text).parameters), ["self", "text"])
        self.assertEqual(inspect.signature(BehaviourEngine.pause).parameters["action"].default, "default")

    def test_reproducible_seed_and_injected_adapters(self):
        first, clock1, keys1 = make_engine(seed=81)
        second, clock2, keys2 = make_engine(seed=81)
        for engine in (first, second):
            engine.pause("reading")
            engine.type_text("Progress complete.\nNext steps ready!")
        self.assertEqual(clock1.sleeps, clock2.sleeps)
        self.assertEqual(keys1.calls, keys2.calls)
        self.assertEqual(list(first.events), list(second.events))
        self.assertTrue(clock1.sleeps)
        self.assertTrue(keys1.calls)

    def test_distinct_seeds_change_samples(self):
        a = TimingModel(random.Random(1))
        b = TimingModel(random.Random(2))
        self.assertNotEqual([a.sample() for _ in range(10)], [b.sample() for _ in range(10)])

    def test_all_distributions_bounded_and_contextual(self):
        model = TimingModel(random.Random(42))
        for action, profile in DEFAULT_PROFILES.items():
            samples = [model.sample(action) for _ in range(2000)]
            self.assertGreaterEqual(min(samples), profile.minimum)
            self.assertLessEqual(max(samples), profile.maximum)
            self.assertGreater(len(set(samples)), 10)
        typing = [model.sample("typing") for _ in range(1000)]
        reading = [model.sample("reading") for _ in range(1000)]
        self.assertGreater(sum(reading), sum(typing) * 5)

    def test_aliases_and_unknown_fallback(self):
        model = TimingModel(variable=False)
        for alias in ("read", "reading"):
            self.assertEqual(model.sample(alias), 4.0)
        self.assertEqual(model.sample(" NAVIGATION "), 1.1)
        self.assertEqual(model.sample("browser_custom_action"), 0.65)

    def test_fixed_delay_baseline(self):
        model = TimingModel(variable=False)
        self.assertEqual([model.sample("typing") for _ in range(4)], [0.13] * 4)

    def test_every_letter_corrected_without_content_corruption(self):
        engine, _, keys = make_engine(correction_probability=1.0)
        text = "Az 12, résumé!\nA\t🙂"
        engine.type_text(text)
        self.assertEqual(keys.text, text)
        count = sum(c.isascii() and c.isalpha() for c in text)
        self.assertEqual(keys.calls.count(("press", "backspace")), count)
        self.assertEqual(engine.events[-1]["details"]["corrections"], count)

    def test_corrections_can_be_disabled(self):
        engine, _, keys = make_engine(enable_typing_corrections=False, correction_probability=1)
        engine.type_text("abc")
        self.assertEqual(keys.text, "abc")
        self.assertNotIn(("press", "backspace"), keys.calls)

    def test_empty_text_does_not_construct_desktop_adapter(self):
        engine = BehaviourEngine()
        engine.type_text("")
        self.assertIsNone(engine._keyboard)
        self.assertEqual(engine.events[-1]["details"]["characters"], 0)

    def test_newline_normalisation_and_boundary_pauses(self):
        engine, _, keys = make_engine()
        engine.type_text("Hello.\r\nWorld!\r")
        self.assertEqual(keys.text, "Hello.\nWorld!\n")
        contexts = [e["details"].get("context") for e in engine.events]
        self.assertEqual(contexts.count("paragraph"), 2)
        self.assertEqual(contexts.count("punctuation"), 2)

    def test_invalid_text_rejected_before_first_keystroke(self):
        engine, _, keys = make_engine()
        for text in ("bad\x00text", "bad\x1btext", "bad\ud800", "x" * 20001):
            with self.assertRaises(ValueError):
                engine.type_text(text)
        with self.assertRaises(TypeError):
            engine.type_text(None)
        self.assertEqual(keys.calls, [])

    def test_cancel_before_typing(self):
        engine, _, keys = make_engine()
        engine.cancel()
        with self.assertRaises(CancelledError):
            engine.type_text("not sent")
        self.assertEqual(keys.text, "")
        self.assertFalse(engine.events[-1]["ok"])

    def test_cancel_during_pause_is_responsive(self):
        engine, clock, _ = make_engine()
        engine.cancel_check = lambda: clock.now >= 0.25
        with self.assertRaises(CancelledError):
            engine.pause("reading")
        self.assertLessEqual(clock.now, 0.31)

    def test_focus_loss_mid_typing_stops_and_guard_is_removed(self):
        engine, _, keys = make_engine(enable_typing_corrections=False)
        def guard():
            if len(keys.text) >= 2:
                raise RuntimeError("Focus lost")
        with self.assertRaisesRegex(RuntimeError, "Focus lost"):
            with engine.input_guard(guard):
                engine.type_text("abcdef")
        self.assertEqual(keys.text, "ab")
        self.assertEqual(engine._guards, [])
        engine.type_text("x")
        self.assertEqual(keys.text, "abx")

    def test_records_are_serializable_without_typed_content(self):
        engine, _, _ = make_engine()
        engine.type_text("PRIVATE-SENTENCE-9284")
        value = json.dumps(list(engine.events))
        self.assertNotIn("PRIVATE", value)
        self.assertTrue(all("ts_utc" in e and "duration_ms" in e for e in engine.events))

    def test_bounded_buffer_stream_and_export_no_overwrite(self):
        delivered = []
        engine, _, _ = make_engine(event_limit=2, event_sink=delivered.append)
        for _ in range(4):
            engine.pause()
        self.assertEqual(len(engine.events), 2)
        self.assertEqual(len(delivered), 4)
        self.assertEqual(engine.dropped_events, 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            engine.export_events(path)
            self.assertEqual(len(path.read_text().splitlines()), 2)
            with self.assertRaises(FileExistsError):
                engine.export_events(path)

    def test_frozen_repo_configuration_supported(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config/settings.json").read_text())
        engine = BehaviourEngine.from_config(config["behaviour"])
        self.assertIsInstance(engine, BehaviourEngine)
        with self.assertRaises(ValueError):
            BehaviourEngine.from_config({"enable_variable_pauses": "false"})
        with self.assertRaises(ValueError):
            BehaviourEngine.from_config({"profile": "unknown"})
        with self.assertRaises(ValueError):
            BehaviourEngine.from_config({"bogus": True})

    def test_metrics_and_invalid_settings(self):
        self.assertEqual(summarise_intervals([1, 1, 1])["cv"], 0)
        self.assertIsNone(summarise_intervals([])["cv"])
        self.assertIsNone(summarise_intervals([0])["cv"])
        for data in ([float("nan")], [-1]):
            with self.assertRaises(ValueError):
                summarise_intervals(data)
        with self.assertRaises(ValueError):
            PauseProfile(2, 1, 3)
        with self.assertRaises(ValueError):
            TypingSettings(float("nan"))
        with self.assertRaises(ValueError):
            BehaviourEngine(event_limit=0)

    def test_keyboard_map_includes_writer_close_keys(self):
        self.assertEqual(WindowsKeyboard.KEYS["f4"], 0x73)
        self.assertEqual(WindowsKeyboard.KEYS["alt"], 0x12)
        self.assertEqual(WindowsKeyboard.KEYS["f12"], 0x7B)

    @unittest.skipIf(sys.platform == "win32", "Non-Windows error path")
    def test_live_input_rejects_non_windows(self):
        with self.assertRaisesRegex(RuntimeError, "Windows VM"):
            WindowsKeyboard()


if __name__ == "__main__":
    unittest.main()
