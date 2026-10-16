"""Tests for scripts/verify_artifacts.py. Synthetic fixtures only; no GUI."""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from xml.sax.saxutils import escape

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SID = "20261014T120000Z-ab12"

_SCRIPTS = str(PROJECT_ROOT / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
import verify_artifacts as verify  # noqa: E402


def write_odt(path: Path, text: str = "Weekly Project Progress Report") -> None:
    xml = (
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        "<office:body><office:text>"
        + "".join(f"<text:p>{escape(part)}</text:p>" for part in text.split("\n"))
        + "</office:text></office:body></office:document-content>"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", xml)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def event(kind, t, data=None, ts="2026-10-14T12:00:00.000Z"):
    stamp = ts if t == 0 else f"2026-10-14T12:00:{min(int(t), 59):02d}.000Z"
    if t >= 60:
        minutes = min(int(t) // 60, 59)
        seconds = int(t) % 60
        stamp = f"2026-10-14T12:{minutes:02d}:{seconds:02d}.000Z"
    return {"ts": stamp, "t": t, "kind": kind, "data": data or {}}


def by_name(verification, name):
    for item in verification.checks:
        if item.name == name:
            return item
    raise AssertionError(f"check {name!r} not found in {[c.name for c in verification.checks]}")


def build_session(
    root: Path,
    *,
    session_id=SID,
    mode="smoke",
    duration=45.0,
    success=True,
    sampling="disabled",
    sites_visited=3,
    links_followed=1,
    browser_success=True,
    browser_errors=None,
    visits=None,
    odt=True,
    corrupt_odt=None,
    include_resources=False,
    eval_passed=True,
    eval_checks=None,
    run_id=None,
    extra_events=None,
    malformed_jsonl=False,
    controller_errors=None,
    write_failures=0,
    include_events=True,
    include_summary=True,
    include_controller=True,
    include_eval=True,
    docx_instead=False,
    empty_events=False,
):
    session_dir = root / session_id
    session_dir.mkdir(parents=True)
    browser_dir = session_dir / "cycle_001_browser"
    office_dir = session_dir / "cycle_001_office"
    browser_dir.mkdir()
    office_dir.mkdir()

    odt_path = office_dir / "Weekly_Project_Report.odt"
    if docx_instead:
        (office_dir / "Weekly_Project_Report.docx").write_bytes(b"PK\x03\x04 fake-docx")
    elif corrupt_odt == "bytes":
        odt_path.write_bytes(b"not-a-zip")
    elif corrupt_odt == "xml":
        with zipfile.ZipFile(odt_path, "w") as archive:
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
            archive.writestr("content.xml", "<not-xml")
    elif odt:
        write_odt(odt_path)

    if visits is None:
        visits = [
            {"url": "https://www.apple.com", "status": "visited"},
            {"url": "https://www.dell.com", "status": "visited"},
            {"url": "https://aws.amazon.com", "status": "visited"},
        ]
    browser_errors = list(browser_errors or [])
    browser_report = {
        "module": "web_browser",
        "success": browser_success,
        "sites_visited": sites_visited,
        "links_followed": links_followed,
        "minimum_links_per_session": 1,
        "browser_closed": True,
        "visits": visits,
        "link_results": [{"site": "apple.com", "link_text": "Mac", "result": "followed", "reason": None}],
        "actions_completed": ["visited:https://www.apple.com", "link_followed:apple.com:Mac", "browser_closed"],
        "errors": browser_errors,
    }
    browser_path = browser_dir / "browser_session.json"
    write_json(browser_path, browser_report)

    office_artifacts = [str(odt_path)] if odt_path.is_file() else []
    results = [
        {
            "module": "browser",
            "cycle": 1,
            "success": browser_success,
            "actions_completed": browser_report["actions_completed"],
            "artifacts": [str(browser_path)],
            "errors": browser_errors,
        },
        {
            "module": "office",
            "cycle": 1,
            "success": bool(odt_path.is_file() and corrupt_odt is None and not docx_instead),
            "actions_completed": ["report_saved_and_verified", "office_closed"],
            "artifacts": office_artifacts,
            "errors": [] if office_artifacts and corrupt_odt is None else ["office document missing"],
        },
    ]
    if not success:
        results[0]["success"] = browser_success
        if not browser_success:
            results[0]["errors"] = browser_errors or ["task reported failure"]

    events = [
        event("session_start", 0.0, {
            "session_id": session_id,
            "scenario": "Weekly Project Progress Report",
            "resource_sampling": sampling,
            "host": "vm-hack23",
            "os": "Windows",
            "python": "3.12.10",
            "process": {"pid": 100, "ppid": 1},
        }),
        event("module_start", 0.2, {"module": "browser"}),
        event("pause", 1.0, {"action": "navigate", "duration_s": 0.8, "module": "browser"}),
        event("result", 20.0, results[0]),
        event("module_end", 20.1, {"module": "browser", "duration_s": 19.9, "ok": browser_success}),
        event("module_start", 20.2, {"module": "office"}),
        event("typing", 30.0, {"chars": 80, "words": 12, "duration_s": 8.0, "module": "office"}),
        event("result", duration - 1 if duration > 2 else 21.0, results[1]),
        event("module_end", duration - 0.5 if duration > 2 else 21.5,
              {"module": "office", "duration_s": 20.0, "ok": results[1]["success"]}),
        event("session_end", duration, {"duration_s": duration}),
    ]
    if include_resources:
        events.insert(-1, event("resource", min(duration / 2, duration), {
            "t": min(duration / 2, duration), "cpu_percent": 4.2, "rss_mb": 85.0,
            "threads": 10, "system_cpu_percent": 12.0, "system_memory_percent": 41.0,
        }))
    if extra_events:
        events.extend(extra_events)
    events.sort(key=lambda item: item["t"])

    if include_events:
        payload = ""
        if not empty_events:
            payload = "\n".join(json.dumps(item) for item in events) + "\n"
            if malformed_jsonl:
                payload += "{not json\n"
        (session_dir / "events.jsonl").write_text(payload, encoding="utf-8")

    resources = {"samples": 0}
    if include_resources:
        resources = {
            "samples": 2,
            "cpu_mean_percent": 4.1,
            "cpu_peak_percent": 6.0,
            "rss_mean_mb": 84.0,
            "rss_peak_mb": 85.0,
        }
        with (session_dir / "resource_samples.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(verify.RESOURCE_FIELDS))
            writer.writeheader()
            writer.writerow({
                "t": 5.0, "cpu_percent": 4.0, "rss_mb": 83.0, "threads": 10,
                "system_cpu_percent": 11.0, "system_memory_percent": 40.0,
            })
            writer.writerow({
                "t": 10.0, "cpu_percent": 4.2, "rss_mb": 85.0, "threads": 10,
                "system_cpu_percent": 12.0, "system_memory_percent": 41.0,
            })

    summary = {
        "session_id": session_id,
        "scenario": "Weekly Project Progress Report",
        "started_at": "2026-10-14T12:00:00.000Z",
        "ended_at": "2026-10-14T12:16:00.000Z" if duration >= 900 else "2026-10-14T12:00:45.000Z",
        "duration_s": duration,
        "module_durations": {"browser": 19.9, "office": 20.0},
        "results": results,
        "event_count": len(events),
        "write_failures": write_failures,
        "resources": resources,
    }
    if include_summary:
        write_json(session_dir / "session_summary.json", summary)

    checks = eval_checks or [
        {"name": "telemetry_complete", "weight": 10, "passed": True, "detail": "ok"},
        {"name": "minimum_duration", "weight": 15, "passed": mode != "full" or duration >= 900, "detail": f"{duration}s"},
        {"name": "modules_succeeded", "weight": 15, "passed": success, "detail": "ok"},
        {"name": "browser_links_followed", "weight": 10, "passed": links_followed >= 1, "detail": f"{links_followed}"},
        {"name": "office_document_created", "weight": 10, "passed": bool(office_artifacts), "detail": "odt"},
    ]
    if not eval_passed:
        checks[2]["passed"] = False
    evaluation = {
        "session_dir": str(session_dir),
        "score": 100.0 if eval_passed else 70.0,
        "passed": eval_passed,
        "checks": checks,
        "metrics": {
            "duration_s": duration,
            "resource_samples": resources["samples"],
            "cpu_mean_percent": resources.get("cpu_mean_percent"),
            "browser_sites_visited": sites_visited,
            "browser_links_followed": links_followed,
            "office_document": str(odt_path) if odt_path.is_file() else None,
        },
        "process_observations": {},
    }
    if include_eval:
        write_json(session_dir / "evaluation_report.json", evaluation)
        (session_dir / "evaluation_report.md").write_text(
            "# PersonaOS session evaluation\n\n"
            f"- Result: **{'PASS' if eval_passed else 'FAIL'}**, score {evaluation['score']}/100\n",
            encoding="utf-8",
        )

    controller = {
        "run_id": run_id if run_id is not None else session_id,
        "mode": mode,
        "success": success,
        "cancelled": False,
        "duration_s": duration,
        "target_duration_s": 0.0 if mode == "smoke" else 960.0,
        "session_dir": str(session_dir),
        "results": results,
        "telemetry": summary if include_summary else None,
        "evaluation": {"score": evaluation["score"], "passed": eval_passed} if include_eval else None,
        "artifacts": [str(browser_path), *office_artifacts],
        "errors": list(controller_errors or []),
    }
    if include_controller:
        write_json(session_dir / "controller_summary.json", controller)
    return session_dir


class VerifyArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {
            "office": {"document_name": "Weekly_Project_Report.odt"},
            "browser": {"minimum_links_per_session": 1},
            "telemetry": {"capture_resource_usage": False},
            "session": {"minimum_duration_seconds": 900, "target_duration_seconds": 960},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, session_dir, config=None):
        return verify.verify_session(session_dir, config if config is not None else self.config)

    def test_valid_smoke_session_passes(self):
        session = build_session(self.root, mode="smoke", duration=45.0)
        result = self.verify(session)
        self.assertFalse(result.failed, result.report())
        self.assertEqual(by_name(result, "timestamps_and_duration").status, verify.PASS)
        self.assertIn("900s not required", by_name(result, "timestamps_and_duration").reason)
        self.assertEqual(by_name(result, "odt_document").status, verify.PASS)
        self.assertEqual(by_name(result, "browser_navigation").status, verify.PASS)

    def test_valid_full_session_with_resources_passes(self):
        session = build_session(
            self.root, mode="full", duration=960.0, sampling="psutil every 5.0s",
            include_resources=True,
        )
        config = dict(self.config)
        config["telemetry"] = {"capture_resource_usage": True}
        result = self.verify(session, config)
        self.assertFalse(result.failed, result.report())
        self.assertEqual(by_name(result, "resource_metrics").status, verify.PASS)
        self.assertEqual(by_name(result, "timestamps_and_duration").status, verify.PASS)

    def test_smoke_short_duration_does_not_require_fifteen_minutes(self):
        session = build_session(self.root, mode="smoke", duration=8.0)
        result = self.verify(session)
        self.assertEqual(by_name(result, "timestamps_and_duration").status, verify.PASS)
        self.assertFalse(result.failed, result.report())

    def test_full_short_duration_fails(self):
        session = build_session(self.root, mode="full", duration=80.0, success=False,
                                controller_errors=["Full run lasted 80s; target is 960s"])
        result = self.verify(session)
        self.assertTrue(result.failed)
        self.assertEqual(by_name(result, "timestamps_and_duration").status, verify.FAIL)
        self.assertIn("15-minute", by_name(result, "timestamps_and_duration").reason)

    def test_missing_files_fail(self):
        session = build_session(self.root)
        (session / "controller_summary.json").unlink()
        (session / "evaluation_report.md").unlink()
        result = self.verify(session)
        self.assertTrue(result.failed)
        self.assertEqual(by_name(result, "required_files").status, verify.FAIL)
        self.assertIn("controller_summary.json", by_name(result, "required_files").reason)

    def test_empty_events_file_fails(self):
        session = build_session(self.root, empty_events=True)
        result = self.verify(session)
        self.assertEqual(by_name(result, "events_jsonl").status, verify.FAIL)

    def test_malformed_json_fails(self):
        session = build_session(self.root)
        (session / "session_summary.json").write_text("{bad", encoding="utf-8")
        result = self.verify(session)
        self.assertEqual(by_name(result, "session_summary_json").status, verify.FAIL)

    def test_malformed_jsonl_fails(self):
        session = build_session(self.root, malformed_jsonl=True)
        result = self.verify(session)
        self.assertEqual(by_name(result, "events_jsonl").status, verify.FAIL)
        self.assertIn("malformed", by_name(result, "events_jsonl").reason)

    def test_mismatched_run_ids_fail(self):
        session = build_session(self.root, run_id="other-session-id")
        result = self.verify(session)
        self.assertEqual(by_name(result, "session_identifiers").status, verify.FAIL)
        self.assertIn("mismatch", by_name(result, "session_identifiers").reason)

    def test_missing_odt_fails(self):
        session = build_session(self.root, odt=False)
        result = self.verify(session)
        self.assertEqual(by_name(result, "office_artifacts").status, verify.FAIL)
        self.assertEqual(by_name(result, "odt_document").status, verify.FAIL)

    def test_docx_is_not_accepted(self):
        session = build_session(self.root, odt=False, docx_instead=True)
        result = self.verify(session)
        self.assertEqual(by_name(result, "office_artifacts").status, verify.FAIL)
        self.assertIn("docx", by_name(result, "office_artifacts").reason.lower())

    def test_corrupt_odt_bytes_fail(self):
        session = build_session(self.root, corrupt_odt="bytes")
        result = self.verify(session)
        self.assertEqual(by_name(result, "odt_document").status, verify.FAIL)
        self.assertIn("ZIP", by_name(result, "odt_document").reason)

    def test_corrupt_odt_xml_fails(self):
        session = build_session(self.root, corrupt_odt="xml")
        result = self.verify(session)
        self.assertEqual(by_name(result, "odt_document").status, verify.FAIL)
        self.assertIn("content.xml", by_name(result, "odt_document").reason.lower() + by_name(result, "odt_document").reason)

    def test_missing_telemetry_fails(self):
        session = build_session(self.root, include_events=False, include_summary=False)
        result = self.verify(session)
        self.assertEqual(by_name(result, "required_files").status, verify.FAIL)
        self.assertEqual(by_name(result, "events_jsonl").status, verify.FAIL)
        self.assertEqual(by_name(result, "session_summary_json").status, verify.FAIL)
        self.assertEqual(by_name(result, "session_start_end").status, verify.FAIL)

    def test_recorded_task_failure_is_surfaced(self):
        session = build_session(
            self.root, success=False, browser_success=False, sites_visited=0, links_followed=0,
            visits=[{"url": "https://www.apple.com", "status": "certificate_error"}],
            browser_errors=["certificate_error:https://www.apple.com"],
            controller_errors=["browser: certificate_error:https://www.apple.com"],
            eval_passed=False,
        )
        result = self.verify(session)
        self.assertTrue(result.failed)
        self.assertEqual(by_name(result, "recorded_failures").status, verify.FAIL)
        self.assertEqual(by_name(result, "browser_navigation").status, verify.FAIL)
        self.assertIn("certificate", by_name(result, "browser_navigation").reason.lower())
        self.assertIn("infrastructure blocker", by_name(result, "browser_navigation").reason.lower())

    def test_evaluator_failure_is_surfaced(self):
        session = build_session(self.root, success=False, eval_passed=False,
                                controller_errors=["Evaluation did not pass; inspect evaluation_report.md"])
        result = self.verify(session)
        self.assertEqual(by_name(result, "evaluator_result").status, verify.FAIL)
        self.assertEqual(by_name(result, "evaluation_present").status, verify.PASS)
        self.assertIn("failure", by_name(result, "evaluator_result").reason.lower())

    def test_missing_resource_csv_ok_when_sampling_disabled(self):
        session = build_session(self.root, sampling="disabled", include_resources=False)
        result = self.verify(session)
        self.assertEqual(by_name(result, "resource_metrics").status, verify.PASS)

    def test_missing_resource_csv_fails_for_full_enabled_sampling(self):
        session = build_session(
            self.root, mode="full", duration=960.0, sampling="psutil every 5.0s",
            include_resources=False,
        )
        result = self.verify(session, {
            **self.config,
            "telemetry": {"capture_resource_usage": True},
        })
        self.assertEqual(by_name(result, "resource_metrics").status, verify.FAIL)

    def test_psutil_unavailable_is_warning_not_pass(self):
        session = build_session(self.root, sampling="unavailable: psutil not installed")
        result = self.verify(session)
        self.assertEqual(by_name(result, "resource_metrics").status, verify.WARN)
        self.assertFalse(result.failed)

    def test_does_not_modify_artifacts(self):
        session = build_session(self.root)
        before = {
            path: (path.stat().st_mtime_ns, path.read_bytes())
            for path in session.rglob("*") if path.is_file()
        }
        self.verify(session)
        after = {
            path: (path.stat().st_mtime_ns, path.read_bytes())
            for path in session.rglob("*") if path.is_file()
        }
        self.assertEqual(before, after)

    def test_cli_exit_codes(self):
        session = build_session(self.root)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = verify.main([str(session), "--config", str(PROJECT_ROOT / "config" / "settings.json")])
        self.assertEqual(code, 0, stdout.getvalue())
        self.assertIn("Result: PASS", stdout.getvalue())

        missing = self.root / "does-not-exist"
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = verify.main([str(missing)])
        self.assertEqual(code, 2)
        self.assertIn("not found", stderr.getvalue())

        broken = build_session(self.root / "failcase", session_id="20261014T130000Z-ffff",
                               success=False, eval_passed=False, odt=False)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = verify.main([str(broken)])
        self.assertEqual(code, 1)
        self.assertIn("Result: FAIL", stdout.getvalue())


class OdtInspectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_odt_has_readable_content_xml(self):
        path = self.root / "report.odt"
        write_odt(path, "Title\nBody")
        self.assertIsNone(verify.inspect_odt(path))
        with zipfile.ZipFile(path) as archive:
            ET.fromstring(archive.read("content.xml"))

    def test_zip_without_content_xml_fails(self):
        path = self.root / "empty.odt"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        self.assertIn("content.xml", verify.inspect_odt(path))


if __name__ == "__main__":
    unittest.main()
