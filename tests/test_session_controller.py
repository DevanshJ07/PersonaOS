"""Integration tests use real telemetry files and fake desktop workers."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.interfaces import TaskResult
from core.session_controller import run_session


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class Behaviour:
    def __init__(self, clock):
        self.clock = clock
        self.cancelled = False

    def pause(self, action="default"):
        self.clock.value += 1

    def type_text(self, text):
        self.clock.value += len(text) / 20

    def cancel(self):
        self.cancelled = True


class Evaluation:
    score = 100
    passed = True


class SessionControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.behaviour = Behaviour(self.clock)
        self.order = []
        self.config = {
            "session": {"scenario": "Project report", "minimum_duration_seconds": 900,
                        "target_duration_seconds": 960},
            "office": {"document_name": "Weekly_Project_Report.odt"},
            "browser": {"minimum_links_per_session": 1},
            "behaviour": {},
            "telemetry": {"capture_resource_usage": False},
        }

    def _browser(self, context):
        self.assertIs(context.behaviour._behaviour, self.behaviour)
        self.order.append("browser")
        context.behaviour.pause("reading")
        context.output_dir.mkdir(parents=True)
        path = context.output_dir / "browser_session.json"
        path.write_text(json.dumps({"sites_visited": 2, "links_followed": 1}), encoding="utf-8")
        return TaskResult(True, ["visited:two_sites"], [str(path)])

    def _office(self, context):
        self.assertIs(context.behaviour._behaviour, self.behaviour)
        self.order.append("office")
        context.behaviour.type_text("Report paragraph")
        context.output_dir.mkdir(parents=True)
        path = context.output_dir / "Weekly_Project_Report.odt"
        path.write_bytes(b"mock-odt")
        return TaskResult(True, ["report_saved"], [str(path)])

    def _run(self, **kwargs):
        return run_session(
            self.config, self.root, clock=self.clock,
            behaviour_factory=lambda config: self.behaviour,
            browser_run=self._browser, office_run=self._office,
            evaluator=lambda *a, **kw: Evaluation(),
            report_writer=lambda evaluation, directory: (),
            **kwargs,
        )

    def test_smoke_runs_both_workers_and_writes_real_telemetry(self):
        result = self._run(mode="smoke")
        self.assertTrue(result["success"], result["errors"])
        self.assertEqual(self.order, ["browser", "office"])
        self.assertEqual(len(result["results"]), 2)
        folder = Path(result["session_dir"])
        self.assertTrue((folder / "events.jsonl").is_file())
        self.assertTrue((folder / "session_summary.json").is_file())
        self.assertTrue((folder / "controller_summary.json").is_file())
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
        self.assertEqual([e["data"]["module"] for e in events if e["kind"] == "module_start"],
                         ["browser", "office"])
        self.assertEqual(events[0]["kind"], "session_start")
        self.assertEqual(events[-1]["kind"], "session_end")
        self.assertTrue(any(e["kind"] == "pause" for e in events))
        self.assertTrue(any(e["kind"] == "typing" for e in events))
        self.assertTrue(any(p.endswith(".odt") for p in result["artifacts"]))

    def test_full_repeats_real_tasks_until_target(self):
        def browser(context):
            result = self._browser(context)
            self.clock.value += 249
            return result

        def office(context):
            result = self._office(context)
            self.clock.value += 249
            return result

        result = run_session(
            self.config, self.root, "full", clock=self.clock,
            behaviour_factory=lambda config: self.behaviour,
            browser_run=browser, office_run=office,
            evaluator=lambda *a, **kw: Evaluation(),
            report_writer=lambda *a: (),
        )
        self.assertTrue(result["success"], result["errors"])
        self.assertGreaterEqual(result["duration_s"], 960)
        self.assertEqual(self.order, ["browser", "office", "browser", "office"])
        self.assertNotEqual(result["results"][0]["artifacts"], result["results"][2]["artifacts"])

    def test_worker_failure_stops_before_next_gui_action_and_cleans_up(self):
        def broken_browser(context):
            self.order.append("browser")
            return TaskResult(False, errors=["failsafe_triggered"])

        result = run_session(
            self.config, self.root, clock=self.clock,
            behaviour_factory=lambda config: self.behaviour,
            browser_run=broken_browser, office_run=self._office,
            evaluator=lambda *a, **kw: Evaluation(), report_writer=lambda *a: (),
        )
        self.assertFalse(result["success"])
        self.assertEqual(self.order, ["browser"])
        self.assertIn("browser: failsafe_triggered", result["errors"])
        self.assertIsNotNone(result["telemetry"])
        self.assertTrue((Path(result["session_dir"]) / "session_summary.json").is_file())

    def test_operator_interrupt_stops_telemetry_and_skips_office(self):
        def interrupted(context):
            raise KeyboardInterrupt()

        result = run_session(
            self.config, self.root, clock=self.clock,
            behaviour_factory=lambda config: self.behaviour,
            browser_run=interrupted, office_run=self._office,
            evaluator=lambda *a, **kw: Evaluation(), report_writer=lambda *a: (),
        )
        self.assertTrue(result["cancelled"])
        self.assertFalse(result["success"])
        self.assertTrue(self.behaviour.cancelled)
        self.assertEqual(self.order, [])
        self.assertIsNotNone(result["telemetry"])

    def test_sessions_have_distinct_directories(self):
        first = self._run()
        second = self._run()
        self.assertNotEqual(first["session_dir"], second["session_dir"])
        self.assertTrue(Path(first["session_dir"], "controller_summary.json").is_file())

    def test_full_mode_rejects_less_than_fifteen_minutes(self):
        self.config["session"]["minimum_duration_seconds"] = 20
        with self.assertRaisesRegex(ValueError, "minimum >=900s"):
            self._run(mode="full")

    def test_real_evaluator_writes_reports_for_smoke(self):
        result = run_session(
            self.config, self.root, clock=self.clock,
            behaviour_factory=lambda config: self.behaviour,
            browser_run=self._browser, office_run=self._office,
        )
        self.assertTrue(result["success"], result["errors"])
        self.assertTrue(Path(result["session_dir"], "evaluation_report.json").is_file())
        self.assertTrue(Path(result["session_dir"], "evaluation_report.md").is_file())
        self.assertIsNotNone(result["evaluation"])
        self.assertIn(str(Path(result["session_dir"], "controller_summary.json")),
                      result["artifacts"])

    def test_main_cli_returns_status_from_controller(self):
        import main

        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        with patch("main.run_session", return_value={
            "success": True, "session_dir": str(self.root), "duration_s": 3, "errors": []
        }) as mocked:
            self.assertEqual(main.main(["--mode", "smoke", "--config", str(config_path),
                                        "--output-dir", str(self.root)]), 0)
            self.assertEqual(mocked.call_args.args[2], "smoke")
        with patch("main.run_session", return_value={
            "success": False, "session_dir": str(self.root), "duration_s": 3,
            "errors": ["browser failed"]
        }):
            self.assertEqual(main.main(["--mode", "smoke", "--config", str(config_path)]), 1)


if __name__ == "__main__":
    unittest.main()
