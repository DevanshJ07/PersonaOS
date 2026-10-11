"""Post-run evaluation of a PersonaOS telemetry session (Member 4).

Reads <session_dir>/events.jsonl and session_summary.json written by
telemetry.collector, scores weighted checks, and writes evaluation_report.json
and evaluation_report.md. Every metric is computed from the agent's own logged
events and resource samples. The score is a team readiness heuristic, not the
judges' rubric and not evidence of "indistinguishability".

Usage:
    python -m telemetry.evaluator [session_dir] [--config path] [--sysmon]
Defaults to the latest session under telemetry.output_directory. Exit code 0
when every check passes, 1 otherwise.
"""

import argparse
import json
import platform
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath

from telemetry.collector import EVENTS_FILE, SUMMARY_FILE

# Thresholds ------------------------------------------------------------------
# Workshop 2 suggested pause CV > 0.4 as a heuristic for varied timing.
PAUSE_CV_MIN = 0.40
MIN_PAUSE_SAMPLES = 5
TYPING_WPM_MIN = 15.0
TYPING_WPM_MAX = 120.0
CHARS_PER_WORD = 5
MAX_IDLE_GAP_SECONDS = 120.0
DEFAULT_MINIMUM_DURATION_SECONDS = 900
BROWSER_SESSION_FILE = "browser_session.json"

CHECK_WEIGHTS = {
    "telemetry_complete": 10,
    "minimum_duration": 15,
    "modules_succeeded": 15,
    "browser_links_followed": 10,
    "office_document_created": 10,
    "pause_variability": 15,
    "typing_speed_human": 10,
    "no_long_idle": 10,
    "no_module_errors": 5,
}

# Events that represent the agent doing something (resource samples do not).
ACTIVITY_KINDS = {"session_start", "session_end", "module_start", "module_end", "module_error",
                  "result", "action", "pause", "typing", "behaviour_call"}
# Events whose duration_s is active work that ends at t (a long pause is idle).
SPAN_KINDS = {"typing", "behaviour_call"}

REPORT_JSON = "evaluation_report.json"
REPORT_MD = "evaluation_report.md"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Sysmon Event ID 1 (read-only, --sysmon) -------------------------------------
SYSMON_CHANNEL = "Microsoft-Windows-Sysmon/Operational"
SYSMON_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
SYSMON_FIELDS = ("UtcTime", "ProcessId", "Image", "CommandLine",
                 "ParentProcessId", "ParentImage", "ParentCommandLine")
# Only processes relevant to the demo; avoids recording unrelated user activity.
SYSMON_IMAGES = {
    "winword.exe", "excel.exe", "outlook.exe",
    "chrome.exe", "msedge.exe", "firefox.exe",
    "python.exe", "pythonw.exe", "py.exe",
}
SYSMON_MAX_EVENTS = 500
SYSMON_MARGIN_SECONDS = 60
SYSMON_TIMEOUT_SECONDS = 30


@dataclass
class Check:
    name: str
    weight: int
    passed: bool
    detail: str


@dataclass
class Evaluation:
    session_dir: str
    score: float
    passed: bool
    checks: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    process_observations: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


# -- loading ------------------------------------------------------------------

def load_events(session_dir):
    """Return (events, malformed_line_count). Bad or truncated lines are skipped."""
    path = Path(session_dir) / EVENTS_FILE
    events, malformed = [], 0
    if not path.is_file():
        return events, malformed
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(event, dict) and "kind" in event:
                events.append(event)
            else:
                malformed += 1
    return events, malformed


def _load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def load_config(path):
    return _load_json(path) or {}


def latest_session(output_dir):
    """Newest session directory (session ids sort chronologically)."""
    root = Path(output_dir)
    if not root.is_dir():
        return None
    sessions = sorted(p for p in root.iterdir() if p.is_dir() and (p / EVENTS_FILE).exists())
    return sessions[-1] if sessions else None


# -- metric helpers -----------------------------------------------------------

def _of_kind(events, kind):
    return [e for e in events if e.get("kind") == kind]


def _data(event):
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stats(values):
    if not values:
        return {"count": 0}
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    return {
        "count": len(values),
        "mean_s": round(mean, 3),
        "stdev_s": round(stdev, 3),
        "min_s": round(min(values), 3),
        "max_s": round(max(values), 3),
        "median_s": round(statistics.median(values), 3),
        "cv": round(stdev / mean, 3) if mean > 0 else None,
    }


def _longest_idle_gap(events, end_t):
    spans = []
    for e in events:
        if e.get("kind") not in ACTIVITY_KINDS:
            continue
        t = _num(e.get("t"))
        if t is None:
            continue
        start = t
        if e["kind"] in SPAN_KINDS:
            start = t - (_num(_data(e).get("duration_s")) or 0.0)
        spans.append((start, t))
    if not spans:
        return None
    spans.sort()
    longest, covered = 0.0, spans[0][1]
    for start, end in spans[1:]:
        longest = max(longest, start - covered)
        covered = max(covered, end)
    if end_t is not None:
        longest = max(longest, end_t - covered)
    return round(max(longest, 0.0), 3)


def _artifacts(results):
    paths = []
    for r in results:
        paths.extend(str(a) for a in (r.get("artifacts") or []))
    return paths


def find_browser_session(session_dir, artifacts):
    """Locate browser_session.json: listed artifacts first, then nearby dirs."""
    for a in artifacts:
        p = Path(a)
        if p.name == BROWSER_SESSION_FILE and p.is_file():
            return p
    session_dir = Path(session_dir)
    nearby = []
    for d in (session_dir, session_dir.parent, session_dir.parent.parent):
        nearby.extend(q for q in (d / BROWSER_SESSION_FILE, *d.glob(f"*/{BROWSER_SESSION_FILE}"))
                      if q.is_file())
    if not nearby:
        return None
    return max(nearby, key=lambda q: q.stat().st_mtime)


def _office_document(artifacts, document_name):
    if not document_name:
        return None
    for a in artifacts:
        p = Path(a)
        if (p.name == document_name or p.name.endswith("_" + document_name)) and p.is_file() \
                and p.stat().st_size > 0:
            return p
    return None


# -- evaluation ---------------------------------------------------------------

def evaluate_session(session_dir, config=None, sysmon=False):
    """Score one session directory against CHECK_WEIGHTS and return an Evaluation."""
    config = config or {}
    session_dir = Path(session_dir)
    events, malformed = load_events(session_dir)
    summary = _load_json(session_dir / SUMMARY_FILE) or {}

    starts, ends = _of_kind(events, "session_start"), _of_kind(events, "session_end")
    results = [_data(e) for e in _of_kind(events, "result")] or list(summary.get("results") or [])
    errors = _of_kind(events, "module_error")
    artifacts = _artifacts(results)

    duration = _num(summary.get("duration_s"))
    if duration is None and ends:
        duration = _num(_data(ends[-1]).get("duration_s"))
    if duration is None and events:
        duration = max((_num(e.get("t")) or 0.0) for e in events)
    duration = duration or 0.0

    pauses = [v for v in (_num(_data(e).get("duration_s")) for e in _of_kind(events, "pause")) if v is not None]
    pause_stats = _stats(pauses)
    by_action = {}
    for e in _of_kind(events, "pause"):
        v = _num(_data(e).get("duration_s"))
        if v is not None:
            by_action.setdefault(str(_data(e).get("action", "default")), []).append(v)

    typing = _of_kind(events, "typing")
    typed_chars = sum(int(_num(_data(e).get("chars")) or 0) for e in typing)
    typed_seconds = sum((_num(_data(e).get("duration_s")) or 0.0) for e in typing)
    wpm = round((typed_chars / CHARS_PER_WORD) / (typed_seconds / 60.0), 1) if typed_seconds > 0 else None

    end_t = _num(ends[-1].get("t")) if ends else None
    idle_gap = _longest_idle_gap(events, end_t)

    samples = [_data(e) for e in _of_kind(events, "resource")]
    cpu = [v for v in (_num(s.get("cpu_percent")) for s in samples) if v is not None]
    rss = [v for v in (_num(s.get("rss_mb")) for s in samples) if v is not None]

    browser_path = find_browser_session(session_dir, artifacts)
    browser = _load_json(browser_path) if browser_path else None
    links = int(_num((browser or {}).get("links_followed")) or 0)
    min_links = int(config.get("browser", {}).get("minimum_links_per_session", 1))
    document_name = config.get("office", {}).get("document_name")
    document = _office_document(artifacts, document_name)
    min_duration = float(config.get("session", {}).get("minimum_duration_seconds",
                                                       DEFAULT_MINIMUM_DURATION_SECONDS))
    write_failures = int(_num(summary.get("write_failures")) or 0)

    metrics = {
        "duration_s": round(duration, 3),
        "event_count": len(events),
        "malformed_lines": malformed,
        "write_failures": write_failures,
        "module_durations": summary.get("module_durations", {}),
        "modules": [{"module": r.get("module"), "success": r.get("success"),
                     "actions": len(r.get("actions_completed") or []),
                     "errors": len(r.get("errors") or [])} for r in results],
        "pauses": pause_stats,
        "pauses_by_action": {k: _stats(v) for k, v in sorted(by_action.items())},
        "typing_events": len(typing),
        "typed_chars": typed_chars,
        "typing_seconds": round(typed_seconds, 3),
        "typing_wpm": wpm,
        "longest_idle_gap_s": idle_gap,
        "resource_samples": len(samples),
        "cpu_mean_percent": round(statistics.fmean(cpu), 2) if cpu else None,
        "cpu_peak_percent": max(cpu) if cpu else None,
        "memory_peak_mb": max(rss) if rss else None,
        "browser_session_file": str(browser_path) if browser_path else None,
        "browser_sites_visited": (browser or {}).get("sites_visited"),
        "browser_links_followed": links if browser else None,
        "office_document": str(document) if document else None,
    }

    checks = []

    def check(name, passed, detail):
        checks.append(Check(name, CHECK_WEIGHTS[name], bool(passed), detail))

    complete = bool(starts) and bool(ends) and bool(summary) and write_failures == 0
    check("telemetry_complete", complete,
          f"session_start={bool(starts)} session_end={bool(ends)} summary={bool(summary)} "
          f"write_failures={write_failures} malformed_lines={malformed}")
    check("minimum_duration", duration >= min_duration,
          f"{duration:.1f}s (minimum {min_duration:.0f}s)")
    failed = [r.get("module") for r in results if not r.get("success")]
    check("modules_succeeded", bool(results) and not failed,
          f"{len(results)} results, failed: {failed or 'none'}" if results else "no module results recorded")
    check("browser_links_followed", browser is not None and links >= min_links,
          f"{links} links followed (minimum {min_links}) in {browser_path}" if browser
          else f"{BROWSER_SESSION_FILE} not found")
    check("office_document_created", document is not None,
          f"{document}" if document else f"{document_name!r} not found among existing artifacts")
    cv = pause_stats.get("cv")
    check("pause_variability", len(pauses) >= MIN_PAUSE_SAMPLES and cv is not None and cv >= PAUSE_CV_MIN,
          f"CV {cv} over {len(pauses)} pauses (minimum {PAUSE_CV_MIN}, >= {MIN_PAUSE_SAMPLES} pauses)")
    check("typing_speed_human", wpm is not None and TYPING_WPM_MIN <= wpm <= TYPING_WPM_MAX,
          f"{wpm} WPM (range {TYPING_WPM_MIN:g}-{TYPING_WPM_MAX:g})" if wpm is not None
          else "no typing events recorded")
    check("no_long_idle", idle_gap is not None and idle_gap <= MAX_IDLE_GAP_SECONDS,
          f"longest gap {idle_gap}s (maximum {MAX_IDLE_GAP_SECONDS:g}s)")
    modules_ran = bool(_of_kind(events, "module_start"))
    check("no_module_errors", modules_ran and not errors,
          "no modules ran" if not modules_ran else "none" if not errors else "; ".join(f"{_data(e).get('module')}: {_data(e).get('error_type')}"
                                              for e in errors))

    total = sum(CHECK_WEIGHTS.values())
    score = round(100.0 * sum(c.weight for c in checks if c.passed) / total, 1)
    evaluation = Evaluation(str(session_dir), score, all(c.passed for c in checks), checks, metrics)

    if sysmon:
        evaluation.process_observations = _sysmon_observations(starts, ends)
    return evaluation


def _sysmon_observations(starts, ends):
    if not starts or not ends:
        return {"available": False, "note": "session start/end timestamps missing"}
    try:
        return query_process_creations(parse_utc(starts[0]["ts"]), parse_utc(ends[-1]["ts"]))
    except Exception as exc:
        return {"available": False, "note": f"{type(exc).__name__}: {exc}"}


# -- Sysmon (read-only) -------------------------------------------------------
# Uses the built-in ``wevtutil qe`` query; it never clears, edits or filters the
# log on disk. Reading the Sysmon channel usually needs an elevated prompt. When
# the log cannot be read, the reason is returned as a note; no values are
# substituted.

def _basename(path):
    return PureWindowsPath(path or "").name.lower()


def parse_utc(value):
    """Parse the collector's ISO 'Z' timestamps into naive UTC datetimes."""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def parse_sysmon_events(xml_text, images=SYSMON_IMAGES):
    """Parse wevtutil /f:xml output into dicts, keeping images of interest."""
    root = ET.fromstring(f"<Events>{xml_text}</Events>")
    records = []
    for event in root.findall("e:Event", SYSMON_NS):
        values = {d.get("Name"): (d.text or "") for d in event.findall("e:EventData/e:Data", SYSMON_NS)}
        record = {name: values.get(name, "") for name in SYSMON_FIELDS}
        if images and _basename(record["Image"]) not in images:
            continue
        records.append(record)
    return records


def summarise_lineage(records):
    """Count observed parent -> child image pairs (by basename)."""
    pairs = {}
    for r in records:
        key = f"{_basename(r['ParentImage']) or '?'} -> {_basename(r['Image'])}"
        pairs[key] = pairs.get(key, 0) + 1
    return dict(sorted(pairs.items(), key=lambda kv: -kv[1]))


def query_process_creations(start_utc, end_utc, runner=subprocess.run, system=platform.system):
    """Return {"source", "available", "note", "records", "lineage"} for the session window."""
    result = {"source": f"wevtutil {SYSMON_CHANNEL} EventID=1", "available": False,
              "note": "", "records": [], "lineage": {}}
    if system() != "Windows":
        result["note"] = "not running on Windows; capture Sysmon evidence manually on the VM"
        return result

    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    start = (start_utc - timedelta(seconds=SYSMON_MARGIN_SECONDS)).strftime(fmt)
    end = (end_utc + timedelta(seconds=SYSMON_MARGIN_SECONDS)).strftime(fmt)
    xpath = (f"*[System[(EventID=1) and TimeCreated[@SystemTime>='{start}' "
             f"and @SystemTime<='{end}']]]")
    cmd = ["wevtutil", "qe", SYSMON_CHANNEL, f"/q:{xpath}", "/f:xml", f"/c:{SYSMON_MAX_EVENTS}"]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=SYSMON_TIMEOUT_SECONDS)
    except Exception as exc:
        result["note"] = f"wevtutil failed: {type(exc).__name__}: {exc}"
        return result
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip().splitlines()
        result["note"] = f"wevtutil exit {proc.returncode}: {message[0] if message else 'no output'}"
        return result
    try:
        records = parse_sysmon_events(proc.stdout)
    except ET.ParseError as exc:
        result["note"] = f"could not parse wevtutil output: {exc}"
        return result
    result.update(available=True, records=records, lineage=summarise_lineage(records),
                  note=f"{len(records)} matching events (cap {SYSMON_MAX_EVENTS})")
    return result


# -- reports ------------------------------------------------------------------

def _render_markdown(ev):
    m = ev.metrics
    p = m.get("pauses", {})
    lines = [
        "# PersonaOS session evaluation",
        "",
        f"- Session: `{ev.session_dir}`",
        f"- Result: **{'PASS' if ev.passed else 'FAIL'}**, score {ev.score}/100",
        "",
        "## Checks",
        "",
        "| Check | Weight | Result | Detail |",
        "|---|---:|---|---|",
    ]
    for c in ev.checks:
        detail = str(c.detail).replace("|", "\\|")
        lines.append(f"| {c.name} | {c.weight} | {'PASS' if c.passed else 'FAIL'} | {detail} |")
    lines += [
        "",
        "## Metrics (measured from agent-logged events)",
        "",
        f"- Duration: {m.get('duration_s')} s; events: {m.get('event_count')}; "
        f"malformed lines: {m.get('malformed_lines')}; write failures: {m.get('write_failures')}",
        f"- Pauses: n={p.get('count')}, mean={p.get('mean_s')} s, stdev={p.get('stdev_s')} s, "
        f"min={p.get('min_s')} s, max={p.get('max_s')} s, CV={p.get('cv')}",
        f"- Typing: {m.get('typed_chars')} chars in {m.get('typing_seconds')} s = {m.get('typing_wpm')} WPM",
        f"- Longest idle gap: {m.get('longest_idle_gap_s')} s",
        f"- Agent CPU mean: {m.get('cpu_mean_percent')} %, peak: {m.get('cpu_peak_percent')} %; "
        f"peak memory: {m.get('memory_peak_mb')} MB ({m.get('resource_samples')} samples)"
        if m.get("resource_samples") else
        "- Agent CPU/RAM: not sampled (psutil missing or capture disabled)",
        f"- Browser: sites visited {m.get('browser_sites_visited')}, links followed "
        f"{m.get('browser_links_followed')}",
        f"- Office document: {m.get('office_document')}",
    ]
    if m.get("pauses_by_action"):
        lines += ["", "| Pause action | n | mean s | stdev s | CV |", "|---|---:|---:|---:|---:|"]
        for action, s in m["pauses_by_action"].items():
            lines.append(f"| {action} | {s.get('count')} | {s.get('mean_s')} | {s.get('stdev_s')} | {s.get('cv')} |")
    if ev.process_observations:
        po = ev.process_observations
        lines += ["", "## Process observations (Sysmon Event ID 1, read-only)", "",
                  f"- Source: {po.get('source', 'n/a')}", f"- Available: {po.get('available')}",
                  f"- Note: {po.get('note')}"]
        for pair, count in (po.get("lineage") or {}).items():
            lines.append(f"- `{pair}`: {count}")
    lines += [
        "",
        "## Limitations",
        "",
        "- Timing metrics come from the agent's own event log, not from Windows kernel or Sysmon telemetry.",
        "- Thresholds are team heuristics (Workshop 2 suggested pause CV > 0.4); passing them does not "
        "show the run is indistinguishable from a human.",
        "- CPU/RAM cover the agent process and its children only; Word and the browser are separate processes. "
        "Use Process Explorer or the Proxmox Summary tab for those.",
        "",
    ]
    return "\n".join(lines)


def write_reports(evaluation, out_dir=None):
    """Write evaluation_report.json and evaluation_report.md; return their paths."""
    out = Path(out_dir or evaluation.session_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out / REPORT_JSON, out / REPORT_MD
    json_path.write_text(json.dumps(evaluation.to_dict(), indent=2, default=repr), encoding="utf-8")
    md_path.write_text(_render_markdown(evaluation), encoding="utf-8")
    return json_path, md_path


# -- CLI ----------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m telemetry.evaluator",
                                     description="Evaluate a PersonaOS telemetry session.")
    parser.add_argument("session_dir", nargs="?", help="session directory (default: latest)")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "settings.json"))
    parser.add_argument("--sysmon", action="store_true",
                        help="also read Sysmon Event ID 1 for the session window (Windows, usually admin)")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.session_dir:
        session_dir = Path(args.session_dir)
    else:
        output = Path(config.get("telemetry", {}).get("output_directory", "outputs/telemetry"))
        if not output.is_absolute():
            output = PROJECT_ROOT / output
        session_dir = latest_session(output)
        if session_dir is None:
            print(f"No telemetry sessions found in {output}", file=sys.stderr)
            return 1
    if not session_dir.is_dir():
        print(f"Session directory not found: {session_dir}", file=sys.stderr)
        return 1

    evaluation = evaluate_session(session_dir, config, sysmon=args.sysmon)
    json_path, md_path = write_reports(evaluation)
    print(md_path.read_text(encoding="utf-8"))
    print(f"Reports: {json_path}\n         {md_path}")
    return 0 if evaluation.passed else 1


if __name__ == "__main__":
    sys.exit(main())
