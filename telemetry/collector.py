"""Passive session telemetry for PersonaOS (Member 4).

The collector records what the agent itself did: module boundaries, results,
pauses, typing bursts and named actions, plus optional low-rate CPU/RSS
samples of the agent process. It never reads keystrokes, typed text, other
users' applications or Windows security logs, and never alters process
lineage.

Output per session, under <output_dir>/<session_id>/:
    events.jsonl          one JSON object per line: ts, t, kind, data
    session_summary.json  written by end_session()
    resource_samples.csv  written by end_session() when samples exist

Telemetry must never break a session: every write error is caught and
counted in ``write_failures`` instead of being raised.
"""

import csv
import json
import os
import platform
import secrets
import statistics
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

EVENTS_FILE = "events.jsonl"
SUMMARY_FILE = "session_summary.json"
RESOURCE_FILE = "resource_samples.csv"

RESOURCE_FIELDS = ("t", "cpu_percent", "rss_mb", "threads", "system_cpu_percent", "system_memory_percent")


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_session_id():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(2)


def _import_psutil():
    """psutil is optional on the offline VM; return None when it is missing."""
    try:
        import psutil
    except Exception:
        return None
    return psutil


def _to_json_line(record):
    """Serialise one record; anything json cannot handle becomes repr()."""
    try:
        return json.dumps(record, default=repr, ensure_ascii=False)
    except (TypeError, ValueError):
        # e.g. non-string dict keys or circular references inside data
        safe = dict(record)
        safe["data"] = {"repr": repr(record.get("data"))}
        return json.dumps(safe, default=repr, ensure_ascii=False)


def _result_to_dict(result):
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    if isinstance(result, dict):
        return dict(result)
    return {
        "success": getattr(result, "success", None),
        "actions_completed": list(getattr(result, "actions_completed", []) or []),
        "artifacts": list(getattr(result, "artifacts", []) or []),
        "errors": list(getattr(result, "errors", []) or []),
    }


class _ResourceSampler(threading.Thread):
    """Samples the agent process (and its children) at a low fixed rate."""

    def __init__(self, collector, psutil, interval):
        super().__init__(name="persona-telemetry-sampler", daemon=True)
        self._collector = collector
        self._psutil = psutil
        self._interval = max(0.01, float(interval))
        self._stop_event = threading.Event()

    def stop(self, timeout=2.0):
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)

    def run(self):
        try:
            proc = self._psutil.Process(os.getpid())
            proc.cpu_percent(None)  # prime; the first reading is always 0.0
            self._psutil.cpu_percent(None)
        except Exception as exc:
            self._collector._log("resource_error", {"error": repr(exc)})
            return
        while not self._stop_event.wait(self._interval):
            try:
                self._collector._add_sample(self._sample(proc))
            except Exception as exc:
                self._collector._log("resource_error", {"error": repr(exc)})

    def _sample(self, proc):
        rss = proc.memory_info().rss
        cpu = proc.cpu_percent(None)
        for child in proc.children(recursive=True):
            try:
                rss += child.memory_info().rss
                cpu += child.cpu_percent(None)
            except Exception:
                continue  # child exited between listing and sampling
        return {
            "cpu_percent": round(cpu, 2),
            "rss_mb": round(rss / (1024 * 1024), 2),
            "threads": proc.num_threads(),
            "system_cpu_percent": self._psutil.cpu_percent(None),
            "system_memory_percent": self._psutil.virtual_memory().percent,
        }


class TelemetryCollector:
    """Thread-safe JSONL event recorder for one PersonaOS session."""

    def __init__(self, output_dir, capture_resource_usage=True,
                 resource_interval_seconds=5.0, clock=time.monotonic, session_id=None):
        self.output_dir = Path(output_dir)
        self.capture_resource_usage = bool(capture_resource_usage)
        self.resource_interval_seconds = float(resource_interval_seconds)
        self.clock = clock
        self.session_id = session_id or _new_session_id()
        self.session_dir = self.output_dir / self.session_id
        self.events_path = self.session_dir / EVENTS_FILE

        self.scenario = None
        self.event_count = 0
        self.write_failures = 0
        self.module_durations = {}
        self.results = []

        self._lock = threading.RLock()
        self._t0 = None
        self._started_at = None
        self._ended = False
        self._summary = None
        self._sampler = None
        self._samples = []
        self._module_stack = []
        self._dir_ready = False

    @classmethod
    def from_config(cls, config, project_root, **kwargs):
        """Build from settings.json: telemetry.output_directory / capture_resource_usage."""
        telemetry = (config or {}).get("telemetry", {}) or {}
        output = Path(telemetry.get("output_directory", "outputs/telemetry"))
        if not output.is_absolute():
            output = Path(project_root) / output
        kwargs.setdefault("capture_resource_usage", telemetry.get("capture_resource_usage", True))
        return cls(output, **kwargs)

    # -- low-level writing -------------------------------------------------

    def _elapsed(self):
        if self._t0 is None:
            return 0.0
        return round(self.clock() - self._t0, 3)

    def _log(self, kind, data=None):
        """Append one event. Never raises; failures are counted."""
        with self._lock:
            record = {"ts": _utc_now(), "t": self._elapsed(), "kind": kind, "data": data or {}}
            try:
                line = _to_json_line(record)
                if not self._dir_ready:
                    self.session_dir.mkdir(parents=True, exist_ok=True)
                    self._dir_ready = True
                with open(self.events_path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                self.write_failures += 1
                return None
            self.event_count += 1
            return record

    def _add_sample(self, sample):
        with self._lock:
            sample = {"t": self._elapsed(), **sample}
            self._samples.append(sample)
        self._log("resource", sample)

    def _current_module(self):
        return self._module_stack[-1] if self._module_stack else None

    # -- public API --------------------------------------------------------

    def start_session(self, scenario=None):
        """Log host/OS/Python and process identity; start the CPU/RSS sampler."""
        with self._lock:
            if self._t0 is not None:
                return
            self._t0 = self.clock()
            self._started_at = _utc_now()
            self.scenario = scenario

        psutil = _import_psutil() if self.capture_resource_usage else None
        self._log("session_start", {
            "session_id": self.session_id,
            "scenario": scenario,
            "host": platform.node(),
            "os": platform.platform(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "process": self._process_identity(psutil),
            "resource_sampling": self._sampling_status(psutil),
        })

        if psutil is not None:
            self._sampler = _ResourceSampler(self, psutil, self.resource_interval_seconds)
            self._sampler.start()

    def _sampling_status(self, psutil):
        if not self.capture_resource_usage:
            return "disabled"
        if psutil is None:
            return "unavailable: psutil not installed"
        return f"psutil every {self.resource_interval_seconds}s"

    @staticmethod
    def _process_identity(psutil):
        """Observed (not asserted) identity of the agent process and its parent."""
        identity = {"pid": os.getpid(), "ppid": os.getppid()}
        if psutil is None:
            return identity
        try:
            proc = psutil.Process(os.getpid())
            parent = proc.parent()
            identity.update({
                "name": proc.name(),
                "cmdline": proc.cmdline(),
                "parent_name": parent.name() if parent else None,
                "parent_exe": parent.exe() if parent else None,
            })
        except Exception as exc:
            identity["error"] = repr(exc)
        return identity

    @contextmanager
    def module(self, name):
        """Log module_start/module_end; on exception log module_error and re-raise."""
        start = self.clock()
        self._module_stack.append(name)
        self._log("module_start", {"module": name})
        ok = True
        try:
            yield self
        except BaseException as exc:
            ok = False
            self._log("module_error", {"module": name, "error_type": type(exc).__name__, "error": str(exc)})
            raise
        finally:
            duration = round(self.clock() - start, 3)
            if self._module_stack and self._module_stack[-1] == name:
                self._module_stack.pop()
            with self._lock:
                self.module_durations[name] = round(self.module_durations.get(name, 0.0) + duration, 3)
            self._log("module_end", {"module": name, "duration_s": duration, "ok": ok})

    def record_result(self, module, result):
        """Log a module's TaskResult (success, milestones, artifacts, errors)."""
        data = {"module": module, **_result_to_dict(result)}
        with self._lock:
            self.results.append(data)
        self._log("result", data)

    def action(self, name, module=None, **data):
        """Log a named high-level action, attributed to the current module by default."""
        self._log("action", {"name": name, "module": module or self._current_module(), **data})

    def end_session(self):
        """Stop sampling, log session_end and write the summary. Idempotent."""
        with self._lock:
            if self._ended:
                return self._summary
            self._ended = True
        if self._sampler is not None:
            self._sampler.stop()

        duration = self._elapsed()
        self._log("session_end", {"duration_s": duration})
        self._write_resource_csv()

        with self._lock:
            summary = {
                "session_id": self.session_id,
                "scenario": self.scenario,
                "started_at": self._started_at,
                "ended_at": _utc_now(),
                "duration_s": duration,
                "module_durations": dict(self.module_durations),
                "results": list(self.results),
                "event_count": self.event_count,
                "write_failures": self.write_failures,
                "resources": self._resource_summary(),
            }
        try:
            self.session_dir.mkdir(parents=True, exist_ok=True)
            (self.session_dir / SUMMARY_FILE).write_text(
                json.dumps(summary, indent=2, default=repr), encoding="utf-8")
        except Exception:
            with self._lock:
                self.write_failures += 1
            summary["write_failures"] = self.write_failures
        self._summary = summary
        return summary

    def _resource_summary(self):
        if not self._samples:
            return {"samples": 0}
        cpu = [s["cpu_percent"] for s in self._samples]
        rss = [s["rss_mb"] for s in self._samples]
        return {
            "samples": len(self._samples),
            "cpu_mean_percent": round(statistics.fmean(cpu), 2),
            "cpu_peak_percent": max(cpu),
            "rss_mean_mb": round(statistics.fmean(rss), 2),
            "rss_peak_mb": max(rss),
        }

    def _write_resource_csv(self):
        if not self._samples:
            return
        try:
            with open(self.session_dir / RESOURCE_FILE, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=RESOURCE_FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(self._samples)
        except Exception:
            with self._lock:
                self.write_failures += 1


class InstrumentedBehaviour:
    """Wraps the shared behaviour engine and logs timing without changing it.

    pause -> "pause" event (action, duration_s)
    type_text -> "typing" event (chars, words, duration_s); text is NOT logged
    anything else -> "behaviour_call" event (method, duration_s)
    """

    def __init__(self, behaviour, collector):
        self._behaviour = behaviour
        self._collector = collector

    def _timed(self, kind, call, data):
        clock = self._collector.clock
        start = clock()
        error = None
        try:
            return call()
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            data["duration_s"] = round(clock() - start, 3)
            data["module"] = self._collector._current_module()
            if error:
                data["error"] = error
            self._collector._log(kind, data)

    def pause(self, action="default", *args, **kwargs):
        return self._timed("pause", lambda: self._behaviour.pause(action, *args, **kwargs),
                           {"action": action})

    def type_text(self, text, *args, **kwargs):
        text_str = text if isinstance(text, str) else str(text)
        return self._timed("typing", lambda: self._behaviour.type_text(text, *args, **kwargs),
                           {"chars": len(text_str), "words": len(text_str.split())})

    def __getattr__(self, name):
        if name.startswith("__") or name in ("_behaviour", "_collector"):
            raise AttributeError(name)
        attr = getattr(self._behaviour, name)
        if not callable(attr):
            return attr

        def wrapper(*args, **kwargs):
            return self._timed("behaviour_call", lambda: attr(*args, **kwargs), {"method": name})

        wrapper.__name__ = name
        return wrapper


def instrument_behaviour(behaviour, collector):
    """Return a behaviour proxy that logs to collector; pass it in SessionContext."""
    return InstrumentedBehaviour(behaviour, collector)
