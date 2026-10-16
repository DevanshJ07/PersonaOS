"""Read-only verification of a PersonaOS session output directory (Member 4).

    python scripts\\verify_artifacts.py <session_dir> [--config config\\settings.json]

Checks the files the session controller, telemetry collector and evaluator
actually write. It never creates placeholder evidence, invents metrics, or
modifies artifacts. Microsoft Word / DOCX is not accepted; office output is
Apache OpenOffice Writer `.odt`.

Exit codes: 0 all required checks passed (warnings allowed), 1 one or more
failures, 2 session directory missing or unreadable.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVENTS_FILE = "events.jsonl"
SUMMARY_FILE = "session_summary.json"
CONTROLLER_FILE = "controller_summary.json"
EVAL_JSON = "evaluation_report.json"
EVAL_MD = "evaluation_report.md"
RESOURCE_FILE = "resource_samples.csv"
BROWSER_SESSION_FILE = "browser_session.json"
ODT_MIME = b"application/vnd.oasis.opendocument.text"
MIN_SITES = 2
MIN_FULL_DURATION_S = 900.0
RESOURCE_FIELDS = (
    "t", "cpu_percent", "rss_mb", "threads",
    "system_cpu_percent", "system_memory_percent",
)
CYCLE_BROWSER = re.compile(r"^cycle_\d{3}_browser$")
CYCLE_OFFICE = re.compile(r"^cycle_\d{3}_office$")
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class CheckResult:
    name: str
    status: str
    reason: str


@dataclass
class Verification:
    session_dir: Path
    checks: list[CheckResult] = field(default_factory=list)
    mode: str | None = None
    run_id: str | None = None

    def add(self, name: str, status: str, reason: str) -> None:
        self.checks.append(CheckResult(name, status, reason))

    @property
    def failed(self) -> bool:
        return any(item.status == FAIL for item in self.checks)

    def counts(self) -> tuple[int, int, int]:
        passed = sum(1 for item in self.checks if item.status == PASS)
        warned = sum(1 for item in self.checks if item.status == WARN)
        failed = sum(1 for item in self.checks if item.status == FAIL)
        return passed, warned, failed

    def report(self) -> str:
        passed, warned, failed = self.counts()
        result = FAIL if failed else PASS
        lines = [
            "PersonaOS artifact verification",
            f"Session: {self.session_dir}",
            f"Mode: {self.mode or 'unknown'}",
            f"Run ID: {self.run_id or 'unknown'}",
            "",
        ]
        for item in self.checks:
            lines.append(f"[{item.status:<4}] {item.name}: {item.reason}")
        lines += [
            "",
            f"Checks: {passed} passed, {warned} warned, {failed} failed",
            f"Result: {result}",
        ]
        return "\n".join(lines)


def _load_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    return data, None


def _nonempty_file(path: Path) -> str | None:
    if not path.is_file():
        return "missing"
    if path.stat().st_size <= 0:
        return "empty"
    return None


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_config(path: Path | None) -> dict:
    if path is None:
        return {}
    data, error = _load_json(path)
    if error or not isinstance(data, dict):
        return {}
    return data


def _cycle_dirs(session_dir: Path, pattern: re.Pattern) -> list[Path]:
    if not session_dir.is_dir():
        return []
    return sorted(p for p in session_dir.iterdir() if p.is_dir() and pattern.match(p.name))


def inspect_odt(path: Path) -> str | None:
    """Return an error string if path is not a readable Writer ODT; else None."""
    if path.is_symlink():
        return "symlink rejected; expected a regular .odt file"
    if not path.is_file():
        return "file not found"
    if path.suffix.lower() != ".odt":
        return f"{path.name} is not an .odt document"
    if path.stat().st_size <= 0:
        return "empty"
    if not zipfile.is_zipfile(path):
        return "not a ZIP/ODT container"
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "mimetype" not in names:
                return "mimetype missing"
            if archive.read("mimetype") != ODT_MIME:
                return "mimetype is not OpenDocument text"
            if "content.xml" not in names:
                return "content.xml missing"
            info = archive.getinfo("content.xml")
            if info.file_size > 5_000_000:
                return "content.xml is unexpectedly large"
            try:
                root = ET.fromstring(archive.read("content.xml"))
            except ET.ParseError as exc:
                return f"content.xml unreadable: {exc}"
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        return f"unreadable ODT: {type(exc).__name__}: {exc}"
    ns = {
        "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
        "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    }
    body = root.find(".//office:body/office:text", ns)
    if body is None:
        return "content.xml has no office:body/office:text"
    return None


def _sampling_state(events: list[dict], summary: dict | None, config: dict) -> str:
    """Return enabled, unavailable, disabled, or unknown from recorded artifacts."""
    for event in events:
        if event.get("kind") == "session_start":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            status = str(data.get("resource_sampling") or "")
            if status.startswith("psutil"):
                return "enabled"
            if status.startswith("unavailable"):
                return "unavailable"
            if status == "disabled":
                return "disabled"
    if summary and isinstance(summary.get("resources"), dict):
        if int(_num(summary["resources"].get("samples")) or 0) > 0:
            return "enabled"
    telemetry = config.get("telemetry") if isinstance(config.get("telemetry"), dict) else {}
    if telemetry.get("capture_resource_usage") is False:
        return "disabled"
    if telemetry.get("capture_resource_usage") is True:
        return "enabled"
    return "unknown"


def _load_events(path: Path) -> tuple[list[dict], int, str | None]:
    problem = _nonempty_file(path)
    if problem:
        return [], 0, problem
    events, malformed = [], 0
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], 0, str(exc)
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(record, dict) or "kind" not in record:
            malformed += 1
            continue
        events.append(record)
    if malformed:
        return events, malformed, f"{malformed} malformed JSONL line(s)"
    if not events:
        return events, 0, "no events"
    return events, 0, None


def _certificate_notes(errors: list[str], visits: list) -> str:
    blob = " ".join(errors).lower()
    statuses = [str(v.get("status", "")).lower() for v in visits if isinstance(v, dict)]
    if "certificate_error" in blob or any("certificate" in s for s in statuses):
        return (" Organizer-acknowledged mock-site certificate expiry (14 Oct 2026); "
                "this is an infrastructure blocker, not evidence of successful browsing.")
    return ""


def verify_session(session_dir: str | Path, config: dict | None = None) -> Verification:
    """Inspect an existing session directory; never writes to it."""
    config = config if isinstance(config, dict) else {}
    root = Path(session_dir).expanduser().resolve()
    result = Verification(session_dir=root)
    if not root.is_dir():
        result.add("required_files", FAIL, f"session directory not found: {root}")
        return result

    office_cfg = config.get("office") if isinstance(config.get("office"), dict) else {}
    browser_cfg = config.get("browser") if isinstance(config.get("browser"), dict) else {}
    expected_odt = office_cfg.get("document_name") or "Weekly_Project_Report.odt"
    min_links = int(_num(browser_cfg.get("minimum_links_per_session")) or 1)

    required = [EVENTS_FILE, SUMMARY_FILE, CONTROLLER_FILE, EVAL_JSON, EVAL_MD]
    missing = [name for name in required if _nonempty_file(root / name)]
    if missing:
        details = ", ".join(f"{name} ({_nonempty_file(root / name)})" for name in missing)
        result.add("required_files", FAIL, details)
    else:
        result.add("required_files", PASS, "core session files exist and are nonempty")

    events, malformed, events_error = _load_events(root / EVENTS_FILE)
    if events_error:
        result.add("events_jsonl", FAIL, events_error)
    else:
        bad_shape = [e for e in events if not {"ts", "t", "kind"} <= set(e)]
        if bad_shape:
            result.add("events_jsonl", FAIL, f"{len(bad_shape)} event(s) missing ts/t/kind")
        else:
            result.add("events_jsonl", PASS, f"{len(events)} events parsed")

    summary, summary_error = _load_json(root / SUMMARY_FILE)
    if summary_error or not isinstance(summary, dict):
        result.add("session_summary_json", FAIL, summary_error or "not a JSON object")
        summary = None
    else:
        needed = ("session_id", "duration_s", "results", "started_at", "ended_at")
        absent = [key for key in needed if key not in summary]
        if absent:
            result.add("session_summary_json", FAIL, f"missing fields: {', '.join(absent)}")
        else:
            result.add("session_summary_json", PASS, f"session_id={summary.get('session_id')}")

    controller, controller_error = _load_json(root / CONTROLLER_FILE)
    if controller_error or not isinstance(controller, dict):
        result.add("controller_summary_json", FAIL, controller_error or "not a JSON object")
        controller = None
    else:
        needed = ("run_id", "mode", "success", "duration_s", "results")
        absent = [key for key in needed if key not in controller]
        mode = controller.get("mode")
        if mode not in {"smoke", "full"}:
            absent.append("mode(smoke|full)")
        if absent:
            result.add("controller_summary_json", FAIL, f"missing/invalid fields: {', '.join(absent)}")
        else:
            result.mode = mode
            result.run_id = str(controller.get("run_id") or "")
            result.add("controller_summary_json", PASS,
                       f"mode={mode} success={controller.get('success')}")

    evaluation, eval_error = _load_json(root / EVAL_JSON)
    if eval_error or not isinstance(evaluation, dict):
        result.add("evaluation_report_json", FAIL, eval_error or "not a JSON object")
        evaluation = None
    else:
        needed = ("score", "passed", "checks")
        absent = [key for key in needed if key not in evaluation]
        if absent or not isinstance(evaluation.get("checks"), list):
            result.add("evaluation_report_json", FAIL,
                       f"missing fields: {', '.join(absent) or 'checks is not a list'}")
        else:
            result.add("evaluation_report_json", PASS,
                       f"score={evaluation.get('score')} passed={evaluation.get('passed')}")

    md_problem = _nonempty_file(root / EVAL_MD)
    if md_problem:
        result.add("evaluation_report_md", FAIL, md_problem)
    else:
        md_text = (root / EVAL_MD).read_text(encoding="utf-8-sig", errors="replace")
        if "PersonaOS session evaluation" not in md_text and "score" not in md_text.lower():
            result.add("evaluation_report_md", WARN, "markdown is nonempty but missing expected headings")
        else:
            result.add("evaluation_report_md", PASS, "evaluation markdown is present")

    folder_id = root.name
    summary_id = str((summary or {}).get("session_id") or "")
    start_ids = []
    for event in events:
        if event.get("kind") == "session_start":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if data.get("session_id"):
                start_ids.append(str(data["session_id"]))
    identities = {value for value in (folder_id, summary_id, result.run_id, *start_ids) if value}
    if not identities:
        result.add("session_identifiers", FAIL, "no session identifier found")
    elif len(identities) > 1:
        result.add("session_identifiers", FAIL,
                   "mismatch: folder={!r} summary={!r} run_id={!r} session_start={!r}".format(
                       folder_id, summary_id, result.run_id, start_ids))
    else:
        result.run_id = result.run_id or folder_id
        result.add("session_identifiers", PASS, f"consistent id {next(iter(identities))}")

    kinds = [event.get("kind") for event in events]
    has_start = "session_start" in kinds
    has_end = "session_end" in kinds
    if has_start and has_end:
        result.add("session_start_end", PASS, "session_start and session_end are present")
    else:
        missing_kinds = [name for name, ok in (("session_start", has_start), ("session_end", has_end)) if not ok]
        result.add("session_start_end", FAIL, f"missing {', '.join(missing_kinds)}")

    _check_time(result, events, summary, controller)
    _check_browser(result, root, controller, summary, min_links)
    _check_office(result, root, controller, summary, expected_odt)
    _check_resources(result, root, events, summary, evaluation, config)
    _check_evaluation(result, controller, evaluation)
    _check_recorded_failures(result, controller, summary, events)
    return result


def _check_time(result: Verification, events: list[dict], summary: dict | None,
                controller: dict | None) -> None:
    problems = []
    previous_t = None
    parsed_ts = 0
    for event in events:
        stamp = _parse_ts(event.get("ts"))
        if stamp is None:
            problems.append("unparseable event timestamp")
            break
        parsed_ts += 1
        t_value = _num(event.get("t"))
        if t_value is None or t_value < -0.001:
            problems.append(f"invalid event t={event.get('t')!r}")
            break
        if previous_t is not None and t_value + 0.001 < previous_t:
            problems.append(f"event t went backwards ({previous_t} -> {t_value})")
            break
        previous_t = t_value
    duration = _num((summary or {}).get("duration_s"))
    controller_duration = _num((controller or {}).get("duration_s"))
    end_duration = None
    for event in events:
        if event.get("kind") == "session_end":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            end_duration = _num(data.get("duration_s"))
    if duration is None or duration < 0:
        problems.append(f"invalid summary duration_s={duration!r}")
    started = _parse_ts((summary or {}).get("started_at"))
    ended = _parse_ts((summary or {}).get("ended_at"))
    if summary is not None and (started is None or ended is None):
        problems.append("summary started_at/ended_at missing or unparseable")
    elif started is not None and ended is not None and ended < started:
        problems.append("ended_at is before started_at")
    if duration is not None and controller_duration is not None and abs(duration - controller_duration) > 5:
        problems.append(
            f"summary duration {duration}s and controller duration {controller_duration}s differ by more than 5s")
    if duration is not None and end_duration is not None and abs(duration - end_duration) > 1:
        problems.append(f"summary duration {duration}s != session_end duration {end_duration}s")

    mode = (controller or {}).get("mode") or result.mode
    if mode == "full":
        target = _num((controller or {}).get("target_duration_s"))
        if target is None:
            target = MIN_FULL_DURATION_S
        required = max(MIN_FULL_DURATION_S, target)
        measured = controller_duration if controller_duration is not None else duration
        if measured is None or measured < required:
            problems.append(
                f"full session lasted {measured}s; required >= {required}s (15-minute demonstration)")
    elif mode == "smoke":
        if duration is not None and duration < 0:
            problems.append("smoke duration is negative")

    if problems:
        result.add("timestamps_and_duration", FAIL, "; ".join(problems))
        return
    mode_note = "smoke: 900s not required" if mode == "smoke" else f"mode={mode or 'unknown'}"
    result.add("timestamps_and_duration", PASS,
               f"{parsed_ts} timestamps valid, duration_s={duration} ({mode_note})")


def _browser_payloads(root: Path, controller: dict | None, summary: dict | None) -> list[tuple[Path | None, dict]]:
    payloads = []
    for folder in _cycle_dirs(root, CYCLE_BROWSER):
        path = folder / BROWSER_SESSION_FILE
        data, error = _load_json(path) if path.is_file() else (None, "missing")
        payloads.append((path if path.is_file() else None, data if isinstance(data, dict) else {"_error": error or "invalid"}))
    if payloads:
        return payloads
    artifacts = []
    for source in ((controller or {}).get("results") or [], (summary or {}).get("results") or []):
        if isinstance(source, list):
            for item in source:
                if isinstance(item, dict):
                    artifacts.extend(item.get("artifacts") or [])
    for artifact in artifacts:
        path = Path(str(artifact))
        if path.name == BROWSER_SESSION_FILE and path.is_file():
            data, error = _load_json(path)
            payloads.append((path, data if isinstance(data, dict) else {"_error": error or "invalid"}))
    return payloads


def _check_browser(result: Verification, root: Path, controller: dict | None,
                   summary: dict | None, min_links: int) -> None:
    folders = _cycle_dirs(root, CYCLE_BROWSER)
    payloads = _browser_payloads(root, controller, summary)
    if not folders and not payloads:
        result.add("browser_artifacts", FAIL, "no cycle_*_browser/browser_session.json found")
        result.add("browser_navigation", FAIL, "no browser session artifact to inspect")
        return

    parse_problems = []
    for path, data in payloads:
        if data.get("_error"):
            parse_problems.append(f"{path or 'unknown'}: {data['_error']}")
    if parse_problems:
        result.add("browser_artifacts", FAIL, "; ".join(parse_problems))
    elif not payloads:
        result.add("browser_artifacts", FAIL, "browser cycle folders exist but browser_session.json is missing")
    else:
        names = ", ".join(p.name if p else "?" for p, _ in payloads)
        result.add("browser_artifacts", PASS, f"{len(payloads)} browser_session.json parsed ({names})")

    usable = [data for _, data in payloads if isinstance(data, dict) and "_error" not in data]
    if not usable:
        result.add("browser_navigation", FAIL, "browser_session.json did not parse")
        return

    reasons = []
    cert_note = ""
    for data in usable:
        sites = int(_num(data.get("sites_visited")) or 0)
        links = int(_num(data.get("links_followed")) or 0)
        visits = data.get("visits") if isinstance(data.get("visits"), list) else []
        visited = [v for v in visits if isinstance(v, dict) and v.get("status") == "visited"]
        errors = [str(e) for e in (data.get("errors") or [])]
        cert_note += _certificate_notes(errors, visits)
        if sites < MIN_SITES:
            reasons.append(f"sites_visited={sites} (need >= {MIN_SITES})")
        if visits and len(visited) < MIN_SITES and sites >= MIN_SITES:
            reasons.append("sites_visited count does not match visits with status=visited")
        if links < min_links:
            reasons.append(f"links_followed={links} (need >= {min_links})")
        if data.get("browser_closed") is False:
            reasons.append("browser_closed is false")
        if data.get("success") is False:
            reasons.append("browser_session.success is false")
        if errors:
            reasons.append("errors: " + "; ".join(errors[:6]))
    if reasons:
        result.add("browser_navigation", FAIL, "; ".join(reasons) + cert_note)
        return
    last = usable[-1]
    result.add("browser_navigation", PASS,
               f"sites_visited={last.get('sites_visited')} links_followed={last.get('links_followed')}")


def _office_odt_paths(root: Path, controller: dict | None, summary: dict | None,
                      expected_name: str) -> list[Path]:
    found: list[Path] = []
    for folder in _cycle_dirs(root, CYCLE_OFFICE):
        found.extend(sorted(p for p in folder.glob("*.odt") if p.is_file()))
    for source in ((controller or {}).get("results") or [], (summary or {}).get("results") or []):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            if item.get("module") not in {"office", "office_worker", None}:
                continue
            for artifact in item.get("artifacts") or []:
                path = Path(str(artifact))
                if path.suffix.lower() == ".odt":
                    found.append(path)
    unique = []
    seen = set()
    for path in found:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    if expected_name and not unique:
        for folder in _cycle_dirs(root, CYCLE_OFFICE):
            candidate = folder / expected_name
            if candidate.is_file():
                unique.append(candidate)
    return unique


def _check_office(result: Verification, root: Path, controller: dict | None,
                  summary: dict | None, expected_name: str) -> None:
    docx = []
    for folder in _cycle_dirs(root, CYCLE_OFFICE):
        docx.extend(folder.glob("*.docx"))
    odt_paths = _office_odt_paths(root, controller, summary, expected_name)
    if not odt_paths:
        extra = ""
        if docx:
            extra = f" found {len(docx)} .docx file(s); Word/DOCX is not the expected format"
        result.add("office_artifacts", FAIL, "no nonempty .odt referenced by office results or cycle folders" + extra)
        result.add("odt_document", FAIL, "no .odt document to inspect")
        return

    existing = [p for p in odt_paths if p.is_file() and p.stat().st_size > 0]
    missing = [p for p in odt_paths if p not in existing]
    if not existing:
        result.add("office_artifacts", FAIL,
                   "office artifacts list .odt paths that are missing or empty: "
                   + ", ".join(p.name for p in missing))
        result.add("odt_document", FAIL, "referenced .odt files are missing or empty")
        return
    expected_stem = Path(expected_name).stem
    names_ok = [
        p for p in existing
        if p.name == expected_name or p.name.startswith(expected_stem + "_") and p.suffix.lower() == ".odt"
    ]
    if expected_name and not names_ok:
        result.add("office_artifacts", WARN,
                   f"found {existing[0].name}; expected filename {expected_name} (or {expected_stem}_NNN.odt)")
    else:
        result.add("office_artifacts", PASS,
                   f"{len(existing)} .odt artifact(s): " + ", ".join(p.name for p in existing))

    odt_errors = []
    for path in existing:
        error = inspect_odt(path)
        if error:
            odt_errors.append(f"{path.name}: {error}")
    if odt_errors:
        result.add("odt_document", FAIL, "; ".join(odt_errors))
    else:
        result.add("odt_document", PASS,
                   f"{len(existing)} ODT ZIP container(s) with readable content.xml")


def _check_resources(result: Verification, root: Path, events: list[dict],
                     summary: dict | None, evaluation: dict | None, config: dict) -> None:
    state = _sampling_state(events, summary, config)
    csv_path = root / RESOURCE_FILE
    csv_problem = _nonempty_file(csv_path)
    samples = 0
    if summary and isinstance(summary.get("resources"), dict):
        samples = int(_num(summary["resources"].get("samples")) or 0)
    metrics = (evaluation or {}).get("metrics") if isinstance((evaluation or {}).get("metrics"), dict) else {}
    mode = result.mode

    if state in {"disabled", "unknown"} and csv_problem:
        note = "resource sampling disabled or not recorded; CSV not required"
        if state == "unknown":
            result.add("resource_metrics", WARN, note)
        else:
            result.add("resource_metrics", PASS, note)
        return
    if state == "unavailable":
        result.add("resource_metrics", WARN,
                   "CPU/RAM sampling unavailable (psutil not installed); not treated as a pass")
        return
    if csv_problem:
        if mode == "smoke":
            result.add("resource_metrics", WARN,
                       f"{RESOURCE_FILE} {csv_problem}; smoke sessions can finish before the 5s sampler")
        else:
            result.add("resource_metrics", FAIL,
                       f"{RESOURCE_FILE} {csv_problem} but sampling was enabled")
        return

    try:
        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                result.add("resource_metrics", FAIL, "resource CSV has no header")
                return
            missing = [name for name in RESOURCE_FIELDS if name not in reader.fieldnames]
            rows = list(reader)
    except OSError as exc:
        result.add("resource_metrics", FAIL, f"could not read {RESOURCE_FILE}: {exc}")
        return
    if missing:
        result.add("resource_metrics", FAIL, f"resource CSV missing columns: {', '.join(missing)}")
        return
    if not rows:
        result.add("resource_metrics", FAIL, "resource CSV has a header but no samples")
        return
    cpu = [_num(row.get("cpu_percent")) for row in rows]
    rss = [_num(row.get("rss_mb")) for row in rows]
    if any(value is None for value in cpu + rss):
        result.add("resource_metrics", FAIL, "resource CSV contains non-numeric CPU/RAM values")
        return
    if samples and samples != len(rows):
        result.add("resource_metrics", WARN,
                   f"CSV has {len(rows)} rows; summary.resources.samples={samples}")
        return
    cpu_note = ""
    if metrics.get("cpu_mean_percent") is None and metrics.get("resource_samples"):
        cpu_note = "; evaluation metrics omit CPU mean"
    result.add("resource_metrics", PASS,
               f"{len(rows)} CPU/RAM samples, peak rss_mb={max(rss)}{cpu_note}")


def _check_evaluation(result: Verification, controller: dict | None, evaluation: dict | None) -> None:
    if evaluation is None:
        result.add("evaluation_present", FAIL, "evaluation_report.json missing or invalid")
        result.add("evaluator_result", FAIL, "no evaluator result to inspect")
        return
    checks = evaluation.get("checks") if isinstance(evaluation.get("checks"), list) else []
    score = evaluation.get("score")
    passed = evaluation.get("passed")
    if _num(score) is None or not isinstance(passed, bool) or not checks:
        result.add("evaluation_present", FAIL, "evaluation report lacks score, passed flag, or checks")
    else:
        controller_eval = (controller or {}).get("evaluation") if isinstance(controller, dict) else None
        mismatch = ""
        if isinstance(controller_eval, dict):
            if controller_eval.get("passed") != passed:
                mismatch = (f"; controller.evaluation.passed={controller_eval.get('passed')} "
                            f"!= report passed={passed}")
                result.add("evaluation_present", FAIL, "controller and evaluation_report.json disagree" + mismatch)
            else:
                result.add("evaluation_present", PASS,
                           f"{len(checks)} checks, score={score}{mismatch}")
        else:
            result.add("evaluation_present", PASS, f"{len(checks)} checks, score={score}")

    if passed is True:
        result.add("evaluator_result", PASS, f"evaluator passed with score {score}")
    elif passed is False:
        failed = []
        for item in checks:
            if isinstance(item, dict) and item.get("passed") is False:
                failed.append(str(item.get("name") or "unnamed"))
        result.add("evaluator_result", FAIL,
                   f"evaluator recorded failure (score={score}); failed checks: {', '.join(failed) or 'unspecified'}")
    else:
        result.add("evaluator_result", FAIL, "evaluation.passed is not a boolean")


def _result_rows(controller: dict | None, summary: dict | None, events: list[dict]) -> list[dict]:
    rows = []
    if isinstance(controller, dict) and isinstance(controller.get("results"), list):
        rows.extend(item for item in controller["results"] if isinstance(item, dict))
    if not rows and isinstance(summary, dict) and isinstance(summary.get("results"), list):
        rows.extend(item for item in summary["results"] if isinstance(item, dict))
    if not rows:
        for event in events:
            if event.get("kind") == "result" and isinstance(event.get("data"), dict):
                rows.append(event["data"])
    return rows


def _check_recorded_failures(result: Verification, controller: dict | None,
                             summary: dict | None, events: list[dict]) -> None:
    rows = _result_rows(controller, summary, events)
    errors = []
    if isinstance(controller, dict):
        errors.extend(str(item) for item in (controller.get("errors") or []))
    failed_modules = []
    for row in rows:
        name = str(row.get("module") or "unknown")
        if row.get("success") is False:
            failed_modules.append(name)
            errors.extend(f"{name}: {message}" for message in (row.get("errors") or ["task reported failure"]))
    module_errors = [
        event for event in events
        if event.get("kind") == "module_error"
    ]
    for event in module_errors:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        errors.append(f"{data.get('module')}: {data.get('error_type')}: {data.get('error')}")

    write_failures = int(_num((summary or {}).get("write_failures")) or 0)
    if write_failures:
        errors.append(f"telemetry write_failures={write_failures}")

    success = None if not isinstance(controller, dict) else controller.get("success")
    if errors or failed_modules or success is False:
        cert = _certificate_notes(errors, [])
        result.add("recorded_failures", FAIL,
                   "; ".join(errors[:8]) + cert if errors else "controller.success is false")
        return
    if not rows:
        result.add("recorded_failures", FAIL, "no module TaskResult records found")
        return
    result.add("recorded_failures", PASS, f"{len(rows)} task result(s), none recorded as failed")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a PersonaOS session output directory (read-only).")
    parser.add_argument("session_dir", type=Path, help="session folder under outputs/telemetry")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "settings.json")
    args = parser.parse_args(argv)

    session_dir = args.session_dir.expanduser()
    if not session_dir.is_dir():
        print(f"PersonaOS setup error: session directory not found: {session_dir}", file=sys.stderr)
        return 2

    config = _load_config(args.config) if args.config else {}
    verification = verify_session(session_dir, config)
    print(verification.report())
    return 1 if verification.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
