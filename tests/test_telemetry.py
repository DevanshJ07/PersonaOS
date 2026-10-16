"""Tests for telemetry.collector and telemetry.evaluator (unittest only)."""

import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.interfaces import TaskResult
from telemetry import TelemetryCollector, collector as collector_mod, instrument_behaviour
from telemetry import evaluator

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG = {
    "session": {"minimum_duration_seconds": 900, "target_duration_seconds": 960},
    "office": {"document_name": "Weekly_Project_Report.docx"},
    "browser": {"websites": ["https://www.apple.com", "https://www.dell.com"], "minimum_links_per_session": 1},
    "telemetry": {"output_directory": "outputs/telemetry", "capture_resource_usage": False},
}

VARIED_PAUSES = [0.6, 2.4, 1.1, 5.8, 0.9, 3.7, 1.6, 7.5, 0.7, 2.9]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeBehaviour:
    """Advances the fake clock instead of sleeping; 5 chars/s typing = 60 WPM."""

    def __init__(self, clock, pauses=VARIED_PAUSES, seconds_per_char=0.2):
        self.clock = clock
        self.pauses = list(pauses)
        self.seconds_per_char = seconds_per_char
        self.calls = []
        self.profile = "office_worker"
        self._i = 0

    def pause(self, action="default"):
        self.calls.append(("pause", action))
        self.clock.advance(self.pauses[self._i % len(self.pauses)])
        self._i += 1

    def type_text(self, text):
        self.calls.append(("type_text", text))
        self.clock.advance(len(text) * self.seconds_per_char)

    def scroll(self, direction, amount=3):
        self.calls.append(("scroll", direction, amount))
        self.clock.advance(0.4)
        return amount

    def explode(self):
        raise RuntimeError("boom")


def read_events(collector):
    return [json.loads(line) for line in collector.events_path.read_text(encoding="utf-8").splitlines()]


def build_session(root, duration=960.0, pauses=VARIED_PAUSES, artifacts_dir=None,
                  module_error=False, links_followed=2):
    """Run a fake two-module session through a real collector; return (collector, behaviour)."""
    clock = FakeClock()
    col = TelemetryCollector(Path(root) / "telemetry", capture_resource_usage=False,
                             clock=clock, session_id="20261011T100000Z-test")
    col.start_session("Weekly Project Progress Report")
    beh = instrument_behaviour(FakeBehaviour(clock, pauses), col)
    out = Path(artifacts_dir or Path(root) / "artifacts")
    out.mkdir(parents=True, exist_ok=True)
    half = duration / 2

    with col.module("web_browser"):
        while clock() - 1000.0 < half:
            beh.pause("reading")
            beh.scroll("down")
            col.action("visited", url="https://www.apple.com")
        browser_file = out / "browser_session.json"
        browser_file.write_text(json.dumps({"links_followed": links_followed, "sites_visited": 3}))
    col.record_result("web_browser", TaskResult(True, ["visited"], [str(browser_file)], []))

    try:
        with col.module("office_worker"):
            while clock() - 1000.0 < duration - 5:
                beh.pause("composing")
                beh.type_text("Status update for the weekly report. ")
            if module_error:
                raise RuntimeError("Word window lost")
            doc = out / "Weekly_Project_Report.docx"
            doc.write_bytes(b"PK\x03\x04 fake docx")
        col.record_result("office_worker", TaskResult(True, ["saved"], [str(doc)], []))
    except RuntimeError as exc:
        col.record_result("office_worker", TaskResult(False, [], [], [str(exc)]))

    clock.now = 1000.0 + duration
    col.end_session()
    return col, beh


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kwargs):
        kwargs.setdefault("capture_resource_usage", False)
        return TelemetryCollector(self.root / "telemetry", clock=self.clock, session_id="s1", **kwargs)

    def test_session_lifecycle_and_summary(self):
        col = self.make()
        col.start_session("demo")
        with col.module("web_browser"):
            self.clock.advance(12.5)
            col.action("visited", url="https://www.dell.com")
        col.record_result("web_browser", TaskResult(True, ["a", "b"], ["x.json"], []))
        self.clock.advance(2.5)
        summary = col.end_session()

        self.assertEqual(col.session_dir, self.root / "telemetry" / "s1")
        events = read_events(col)
        self.assertEqual([e["kind"] for e in events],
                         ["session_start", "module_start", "action", "module_end", "result", "session_end"])
        for e in events:
            self.assertTrue(e["ts"].endswith("Z"))
            self.assertEqual(set(e), {"ts", "t", "kind", "data"})
        start = events[0]["data"]
        for key in ("host", "os", "python", "process", "resource_sampling"):
            self.assertIn(key, start)
        self.assertEqual(start["resource_sampling"], "disabled")
        self.assertEqual(events[2]["data"]["module"], "web_browser")
        self.assertEqual(events[3]["data"]["duration_s"], 12.5)
        self.assertEqual(events[5]["t"], 15.0)

        on_disk = json.loads((col.session_dir / "session_summary.json").read_text())
        self.assertEqual(on_disk, summary)
        self.assertEqual(summary["duration_s"], 15.0)
        self.assertEqual(summary["module_durations"], {"web_browser": 12.5})
        self.assertEqual(summary["results"][0]["artifacts"], ["x.json"])
        self.assertEqual(summary["event_count"], 6)
        self.assertEqual(summary["write_failures"], 0)

    def test_from_config(self):
        col = TelemetryCollector.from_config(CONFIG, self.root)
        self.assertEqual(col.output_dir, self.root / "outputs" / "telemetry")
        self.assertFalse(col.capture_resource_usage)

    def test_module_error_logged_and_reraised(self):
        col = self.make()
        col.start_session("demo")
        with self.assertRaises(ValueError):
            with col.module("office_worker"):
                self.clock.advance(3)
                raise ValueError("save dialog missing")
        kinds = [e["kind"] for e in read_events(col)]
        self.assertEqual(kinds, ["session_start", "module_start", "module_error", "module_end"])
        err = read_events(col)[2]["data"]
        self.assertEqual((err["module"], err["error_type"], err["error"]),
                         ("office_worker", "ValueError", "save dialog missing"))
        self.assertFalse(read_events(col)[3]["data"]["ok"])
        self.assertEqual(col.module_durations["office_worker"], 3)

    def test_end_session_idempotent(self):
        col = self.make()
        col.start_session("demo")
        first = col.end_session()
        self.clock.advance(50)
        second = col.end_session()
        self.assertIs(first, second)
        self.assertEqual([e["kind"] for e in read_events(col)].count("session_end"), 1)

    def test_unserialisable_values_use_repr(self):
        col = self.make()
        col.start_session("demo")
        marker = object()
        col.action("odd", obj=marker, items={3}, path=Path("a/b"))
        col.action("bad_keys", mapping={(1, 2): "tuple key"})
        events = read_events(col)
        self.assertEqual(events[1]["data"]["obj"], repr(marker))
        self.assertEqual(events[1]["data"]["items"], repr({3}))
        self.assertIn("tuple key", events[2]["data"]["repr"])
        self.assertEqual(col.write_failures, 0)

    def test_write_failure_counted_not_raised(self):
        blocker = self.root / "blocker"
        blocker.write_text("not a directory")
        col = TelemetryCollector(blocker / "telemetry", capture_resource_usage=False, clock=self.clock)
        col.start_session("demo")
        with col.module("web_browser"):
            col.action("visited")
        col.record_result("web_browser", TaskResult(True))
        summary = col.end_session()
        self.assertGreaterEqual(summary["write_failures"], 6)
        self.assertEqual(col.event_count, 0)

    def test_thread_safe_writes(self):
        col = self.make()
        col.start_session("demo")
        threads = [threading.Thread(target=lambda: [col.action("tick", i=i) for i in range(100)])
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        col.end_session()
        events = read_events(col)  # every line must parse
        self.assertEqual(sum(e["kind"] == "action" for e in events), 800)

    def test_psutil_missing_degrades(self):
        with mock.patch.object(collector_mod, "_import_psutil", return_value=None):
            col = self.make(capture_resource_usage=True)
            col.start_session("demo")
            summary = col.end_session()
        self.assertEqual(read_events(col)[0]["data"]["resource_sampling"], "unavailable: psutil not installed")
        self.assertEqual(summary["resources"], {"samples": 0})

    def test_resource_sampler_with_fake_psutil(self):
        proc = mock.Mock()
        proc.cpu_percent.return_value = 4.0
        proc.memory_info.return_value = SimpleNamespace(rss=50 * 1024 * 1024)
        proc.children.return_value = []
        proc.num_threads.return_value = 3
        proc.name.return_value = "python.exe"
        proc.parent.return_value = None
        fake = SimpleNamespace(Process=lambda pid: proc, cpu_percent=lambda interval=None: 10.0,
                               virtual_memory=lambda: SimpleNamespace(percent=40.0))
        with mock.patch.object(collector_mod, "_import_psutil", return_value=fake):
            col = self.make(capture_resource_usage=True, resource_interval_seconds=0.01)
            col.start_session("demo")
            deadline = threading.Event()
            for _ in range(200):
                if col._samples:
                    break
                deadline.wait(0.01)
            summary = col.end_session()
        self.assertGreater(summary["resources"]["samples"], 0)
        self.assertEqual(summary["resources"]["rss_peak_mb"], 50.0)
        self.assertTrue((col.session_dir / "resource_samples.csv").read_text().startswith("t,cpu_percent"))
        self.assertFalse(col._sampler.is_alive())


class InstrumentedBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.col = TelemetryCollector(self.tmp.name, capture_resource_usage=False, clock=self.clock,
                                      session_id="s")
        self.col.start_session("demo")
        self.inner = FakeBehaviour(self.clock, pauses=[1.5])
        self.beh = instrument_behaviour(self.inner, self.col)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pause_and_typing_logged_without_text(self):
        with self.col.module("office_worker"):
            self.beh.pause("reading")
            self.beh.type_text("secret words here")
        events = read_events(self.col)
        pause = next(e for e in events if e["kind"] == "pause")["data"]
        typing = next(e for e in events if e["kind"] == "typing")["data"]
        self.assertEqual(pause, {"action": "reading", "duration_s": 1.5, "module": "office_worker"})
        self.assertEqual((typing["chars"], typing["words"], typing["duration_s"]), (17, 3, 3.4))
        self.assertNotIn("secret", self.col.events_path.read_text())
        self.assertEqual(self.inner.calls, [("pause", "reading"), ("type_text", "secret words here")])

    def test_default_pause_action(self):
        self.beh.pause()
        self.assertEqual(self.inner.calls, [("pause", "default")])

    def test_other_methods_forwarded_and_logged(self):
        self.assertEqual(self.beh.scroll("down", amount=5), 5)
        self.assertEqual(self.beh.profile, "office_worker")
        call = [e for e in read_events(self.col) if e["kind"] == "behaviour_call"]
        self.assertEqual(len(call), 1)
        self.assertEqual(call[0]["data"]["method"], "scroll")
        self.assertEqual(call[0]["data"]["duration_s"], 0.4)
        with self.assertRaises(RuntimeError):
            self.beh.explode()
        self.assertIn("boom", read_events(self.col)[-1]["data"]["error"])
        with self.assertRaises(AttributeError):
            self.beh.missing_method


class EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def failed(self, ev):
        return sorted(c.name for c in ev.checks if not c.passed)

    def test_weights_total_100(self):
        self.assertEqual(sum(evaluator.CHECK_WEIGHTS.values()), 100)

    def test_good_960s_session_scores_100(self):
        col, _ = build_session(self.root)
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertEqual(self.failed(ev), [])
        self.assertEqual(ev.score, 100.0)
        self.assertTrue(ev.passed)
        m = ev.metrics
        self.assertEqual(m["duration_s"], 960.0)
        self.assertAlmostEqual(m["typing_wpm"], 60.0, places=1)
        self.assertGreaterEqual(m["pauses"]["cv"], evaluator.PAUSE_CV_MIN)
        self.assertLessEqual(m["longest_idle_gap_s"], evaluator.MAX_IDLE_GAP_SECONDS)
        self.assertEqual(m["browser_links_followed"], 2)
        self.assertTrue(m["office_document"].endswith("Weekly_Project_Report.docx"))
        self.assertIsNone(m["cpu_mean_percent"])

    def test_short_session_fails_minimum_duration(self):
        col, _ = build_session(self.root, duration=300)
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertEqual(self.failed(ev), ["minimum_duration"])
        self.assertFalse(ev.passed)
        self.assertEqual(ev.score, 85.0)

    def test_constant_pauses_fail_only_variability(self):
        col, _ = build_session(self.root, pauses=[2.0])
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertEqual(self.failed(ev), ["pause_variability"])
        self.assertEqual(ev.metrics["pauses"]["cv"], 0.0)

    def test_module_error_and_missing_document(self):
        col, _ = build_session(self.root, module_error=True)
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertEqual(self.failed(ev), ["modules_succeeded", "no_module_errors", "office_document_created"])

    def test_no_links_followed(self):
        col, _ = build_session(self.root, links_followed=0)
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertEqual(self.failed(ev), ["browser_links_followed"])

    def test_browser_session_found_nearby_when_artifact_path_moved(self):
        col, _ = build_session(self.root)
        moved = Path(json.loads((col.session_dir / "session_summary.json").read_text())
                     ["results"][0]["artifacts"][0])
        target = col.session_dir.parent / "web_browser"
        target.mkdir()
        moved.rename(target / "browser_session.json")
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertNotIn("browser_links_followed", self.failed(ev))
        self.assertEqual(Path(ev.metrics["browser_session_file"]), target / "browser_session.json")

    def test_long_idle_gap_detected(self):
        col, _ = build_session(self.root, pauses=[1.0, 2.0, 130.0, 0.5, 4.0])
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertIn("no_long_idle", self.failed(ev))
        self.assertGreaterEqual(ev.metrics["longest_idle_gap_s"], 130.0)

    def test_truncated_and_malformed_lines_tolerated(self):
        col, _ = build_session(self.root)
        with open(col.events_path, "a", encoding="utf-8") as fh:
            fh.write('{"ts": "2026-10-11T10:16:00Z", "t": 96')
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertEqual(ev.metrics["malformed_lines"], 1)
        self.assertEqual(ev.score, 100.0)

    def test_malformed_rows_mid_log_skipped(self):
        col, _ = build_session(self.root)
        lines = col.events_path.read_text(encoding="utf-8").splitlines()
        bad = ["not json at all", "[1, 2, 3]", '{"no_kind": true}', ""]
        col.events_path.write_text("\n".join(lines[:5] + bad + lines[5:]) + "\n", encoding="utf-8")
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertEqual(ev.metrics["malformed_lines"], 3)
        self.assertEqual(ev.metrics["event_count"], len(lines))
        self.assertEqual(ev.score, 100.0)

    def test_truncated_session_end_is_incomplete(self):
        col, _ = build_session(self.root)
        lines = col.events_path.read_text(encoding="utf-8").splitlines()
        col.events_path.write_text("\n".join(lines[:-1]) + "\n" + lines[-1][:20], encoding="utf-8")
        (col.session_dir / "session_summary.json").unlink()
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        self.assertIn("telemetry_complete", self.failed(ev))

    def test_empty_session_dir(self):
        empty = self.root / "telemetry" / "empty"
        empty.mkdir(parents=True)
        ev = evaluator.evaluate_session(empty, CONFIG)
        self.assertEqual(ev.score, 0.0)
        self.assertFalse(ev.passed)

    def test_reports_written_and_latest_session(self):
        col, _ = build_session(self.root)
        older = col.output_dir / "20261010T090000Z-aaaa"
        older.mkdir()
        (older / "events.jsonl").write_text("")
        (col.output_dir / "zz-no-events").mkdir()
        self.assertEqual(evaluator.latest_session(col.output_dir), col.session_dir)
        self.assertIsNone(evaluator.latest_session(self.root / "missing"))

        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        json_path, md_path = evaluator.write_reports(ev)
        self.assertEqual(json.loads(json_path.read_text())["score"], 100.0)
        md = md_path.read_text()
        for name in evaluator.CHECK_WEIGHTS:
            self.assertIn(name, md)
        self.assertIn("PASS", md)

    def test_cli_exit_codes(self):
        cfg = self.root / "settings.json"
        cfg.write_text(json.dumps(CONFIG))
        good, _ = build_session(self.root / "good")
        short, _ = build_session(self.root / "short", duration=200)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(evaluator.main([str(good.session_dir), "--config", str(cfg)]), 0)
            self.assertEqual(evaluator.main([str(short.session_dir), "--config", str(cfg)]), 1)
        self.assertTrue((good.session_dir / "evaluation_report.md").exists())

    def test_cli_module_defaults_to_latest_session(self):
        col, _ = build_session(self.root)
        cfg = dict(CONFIG, telemetry={"output_directory": str(col.output_dir)})
        cfg_path = self.root / "settings.json"
        cfg_path.write_text(json.dumps(cfg))
        proc = subprocess.run([sys.executable, "-m", "telemetry.evaluator", "--config", str(cfg_path)],
                              cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("RuntimeWarning", proc.stderr)
        self.assertIn(str(col.session_dir), proc.stdout)

    def test_end_to_end_collector_into_evaluator(self):
        col, beh = build_session(self.root)
        self.assertTrue(any(c[0] == "type_text" for c in beh._behaviour.calls))
        ev = evaluator.evaluate_session(col.session_dir, CONFIG)
        summary = json.loads((col.session_dir / "session_summary.json").read_text())
        self.assertEqual(ev.metrics["event_count"], summary["event_count"])
        self.assertEqual(ev.metrics["module_durations"], summary["module_durations"])
        self.assertTrue(ev.passed)


SYSMON_XML = """
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>1</EventID></System>
<EventData><Data Name='UtcTime'>2026-10-11 10:00:01.000</Data><Data Name='ProcessId'>4242</Data>
<Data Name='Image'>C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe</Data>
<Data Name='CommandLine'>chrome.exe</Data><Data Name='ParentProcessId'>1200</Data>
<Data Name='ParentImage'>C:\\Windows\\explorer.exe</Data><Data Name='ParentCommandLine'>explorer.exe</Data>
</EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>1</EventID></System>
<EventData><Data Name='Image'>C:\\Windows\\System32\\notepad.exe</Data>
<Data Name='ParentImage'>C:\\Windows\\explorer.exe</Data></EventData></Event>
"""

SYSMON_XML_WRITER = """
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>1</EventID></System>
<EventData><Data Name='UtcTime'>2026-10-11 10:00:01.000</Data><Data Name='ProcessId'>4242</Data>
<Data Name='Image'>C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe</Data>
<Data Name='CommandLine'>chrome.exe</Data><Data Name='ParentProcessId'>1200</Data>
<Data Name='ParentImage'>C:\\Windows\\explorer.exe</Data><Data Name='ParentCommandLine'>explorer.exe</Data>
</EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>1</EventID></System>
<EventData><Data Name='UtcTime'>2026-10-11 10:00:02.000</Data><Data Name='ProcessId'>5100</Data>
<Data Name='Image'>C:\\Program Files\\OpenOffice 4\\program\\soffice.exe</Data>
<Data Name='CommandLine'>"C:\\Program Files\\OpenOffice 4\\program\\soffice.exe" -writer</Data>
<Data Name='ParentProcessId'>1200</Data>
<Data Name='ParentImage'>C:\\Windows\\explorer.exe</Data><Data Name='ParentCommandLine'>explorer.exe</Data>
</EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>1</EventID></System>
<EventData><Data Name='UtcTime'>2026-10-11 10:00:02.100</Data><Data Name='ProcessId'>5101</Data>
<Data Name='Image'>C:\\Program Files\\OpenOffice 4\\program\\soffice.bin</Data>
<Data Name='CommandLine'>"C:\\Program Files\\OpenOffice 4\\program\\soffice.bin" -writer</Data>
<Data Name='ParentProcessId'>5100</Data>
<Data Name='ParentImage'>C:\\Program Files\\OpenOffice 4\\program\\soffice.exe</Data>
<Data Name='ParentCommandLine'>"C:\\Program Files\\OpenOffice 4\\program\\soffice.exe" -writer</Data>
</EventData></Event>
<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'><System><EventID>1</EventID></System>
<EventData><Data Name='Image'>C:\\Windows\\System32\\notepad.exe</Data>
<Data Name='ParentImage'>C:\\Windows\\explorer.exe</Data></EventData></Event>
"""


class SysmonTests(unittest.TestCase):
    def test_parse_filters_images_and_summarises_lineage(self):
        records = evaluator.parse_sysmon_events(SYSMON_XML)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["ProcessId"], "4242")
        self.assertEqual(evaluator.summarise_lineage(records), {"explorer.exe -> chrome.exe": 1})

    def test_sysmon_images_keep_existing_names_and_openoffice_writer(self):
        self.assertTrue(
            {"winword.exe", "excel.exe", "outlook.exe",
             "chrome.exe", "msedge.exe", "firefox.exe",
             "python.exe", "pythonw.exe", "py.exe",
             "soffice.exe", "soffice.bin"}.issubset(evaluator.SYSMON_IMAGES)
        )

    def test_parse_keeps_openoffice_writer_and_drops_unrelated(self):
        records = evaluator.parse_sysmon_events(SYSMON_XML_WRITER)
        images = [evaluator._basename(r["Image"]) for r in records]
        self.assertEqual(images, ["chrome.exe", "soffice.exe", "soffice.bin"])
        self.assertNotIn("notepad.exe", images)
        self.assertEqual(evaluator.summarise_lineage(records), {
            "explorer.exe -> chrome.exe": 1,
            "explorer.exe -> soffice.exe": 1,
            "soffice.exe -> soffice.bin": 1,
        })

    def test_non_windows_reports_gap(self):
        start = evaluator.parse_utc("2026-10-11T10:00:00.000Z")
        res = evaluator.query_process_creations(start, start, system=lambda: "Darwin")
        self.assertFalse(res["available"])
        self.assertEqual(res["records"], [])
        self.assertIn("manually", res["note"])

    def test_access_denied_reports_gap(self):
        start = evaluator.parse_utc("2026-10-11T10:00:00.000Z")
        runner = mock.Mock(return_value=SimpleNamespace(returncode=5, stdout="", stderr="Access is denied.\n"))
        res = evaluator.query_process_creations(start, start, runner=runner, system=lambda: "Windows")
        self.assertFalse(res["available"])
        self.assertIn("Access is denied", res["note"])
        cmd = runner.call_args[0][0]
        self.assertEqual(cmd[:3], ["wevtutil", "qe", evaluator.SYSMON_CHANNEL])
        self.assertIn("2026-10-11T09:59:00.000Z", cmd[3])

    def test_windows_success(self):
        start = evaluator.parse_utc("2026-10-11T10:00:00.000Z")
        runner = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=SYSMON_XML, stderr=""))
        res = evaluator.query_process_creations(start, start, runner=runner, system=lambda: "Windows")
        self.assertTrue(res["available"])
        self.assertEqual(res["lineage"], {"explorer.exe -> chrome.exe": 1})


if __name__ == "__main__":
    unittest.main()
