"""Run the visible PersonaOS tasks as one measured desktop session.

Only one worker has the desktop at a time. The collector owns the canonical
session_summary.json; this controller writes controller_summary.json alongside it.
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import time

from behaviour.interaction import BehaviourEngine
from core.interfaces import SessionContext, TaskResult
from modules import office_worker, web_browser
from telemetry.collector import TelemetryCollector, instrument_behaviour
from telemetry.evaluator import evaluate_session, write_reports


def _duration(config: dict, mode: str) -> float:
    if mode not in {"smoke", "full"}:
        raise ValueError("mode must be 'smoke' or 'full'")
    session = config.get("session", {})
    if not isinstance(session, dict):
        raise ValueError("session settings must be an object")
    if mode == "smoke":
        return 0.0
    minimum = float(session.get("minimum_duration_seconds", 900))
    target = float(session.get("target_duration_seconds", 960))
    if minimum < 900 or target < minimum:
        raise ValueError("full mode requires minimum >=900s and target >= minimum")
    return target


def run_session(config: dict, output_root: str | Path, mode: str = "smoke", *,
                clock=time.monotonic, collector_factory=TelemetryCollector,
                behaviour_factory=BehaviourEngine.from_config,
                browser_run=web_browser.run, office_run=office_worker.run,
                evaluator=evaluate_session, report_writer=write_reports) -> dict:
    """Execute and evaluate a session; return a serializable controller summary.

    The optional callables permit end-to-end tests without a Windows desktop.
    A full run repeats actual browser and office tasks until the configured
    target, with a finite cycle limit. It never pads duration with an idle wait.
    """
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    target = _duration(config, mode)
    if not isinstance(config.get("telemetry", {}), dict):
        raise ValueError("telemetry settings must be an object")
    root = Path(output_root).expanduser().resolve()
    collector = collector_factory(
        root, capture_resource_usage=config.get("telemetry", {}).get("capture_resource_usage", True),
        clock=clock,
    )
    session_dir = Path(collector.session_dir)
    if session_dir.exists():
        raise FileExistsError(f"Session directory already exists: {session_dir}")

    errors: list[str] = []
    results: list[dict] = []
    artifacts: list[str] = []
    cancelled = False
    started = False
    telemetry_summary = None
    start = clock()
    max_cycles = 30

    try:
        started = True
        collector.start_session(config.get("session", {}).get("scenario", "Weekly Project Progress Report"))
        behaviour = instrument_behaviour(
            behaviour_factory(config.get("behaviour", {})), collector
        )
        cycle = 0
        while True:
            cycle += 1
            failed = False
            for name, worker in (("browser", browser_run), ("office", office_run)):
                context = SessionContext(config, session_dir / f"cycle_{cycle:03d}_{name}", behaviour)
                try:
                    with collector.module(name):
                        result = worker(context)
                    if not isinstance(result, TaskResult):
                        raise TypeError(f"{name} returned {type(result).__name__}, expected TaskResult")
                except KeyboardInterrupt:
                    cancelled = True
                    errors.append(f"{name}: cancelled by operator")
                    behaviour.cancel()
                    break
                except Exception as exc:
                    result = TaskResult(False, errors=[f"{type(exc).__name__}: {exc}"])
                collector.record_result(name, result)
                data = {"module": name, "cycle": cycle, **asdict(result)}
                results.append(data)
                artifacts.extend(result.artifacts)
                if not result.success:
                    failed = True
                    errors.extend(f"{name}: {message}" for message in result.errors)
                    if not result.errors:
                        errors.append(f"{name}: task reported failure")
                    if any("KeyboardInterrupt" in message or "cancelled" in message.lower()
                           for message in result.errors):
                        cancelled = True
                        behaviour.cancel()
                    break
            if cancelled or failed or mode == "smoke" or clock() - start >= target:
                break
            if cycle >= max_cycles:
                errors.append(f"Full run stopped after {max_cycles} task cycles before target duration")
                break
    except KeyboardInterrupt:
        cancelled = True
        errors.append("Session cancelled by operator")
    except Exception as exc:
        errors.append(f"Controller error: {type(exc).__name__}: {exc}")
    finally:
        if started:
            try:
                telemetry_summary = collector.end_session()
            except Exception as exc:
                errors.append(f"Telemetry cleanup failed: {type(exc).__name__}: {exc}")

    duration_s = round(clock() - start, 3)
    if mode == "full" and duration_s < target and not cancelled:
        errors.append(f"Full run lasted {duration_s}s; target is {target}s")
    if telemetry_summary and telemetry_summary.get("write_failures"):
        errors.append(f"Telemetry had {telemetry_summary['write_failures']} write failures")
    if started and telemetry_summary is None:
        errors.append("Telemetry did not produce a session summary")

    evaluation = None
    if started and telemetry_summary is not None:
        try:
            evaluation_config = dict(config)
            if mode == "smoke":
                evaluation_config["session"] = {
                    **config.get("session", {}), "minimum_duration_seconds": 0
                }
            evaluation = evaluator(session_dir, evaluation_config, sysmon=False)
            reports = report_writer(evaluation, session_dir)
            artifacts.extend(str(p) for p in reports)
            if mode == "full" and not evaluation.passed:
                errors.append("Evaluation did not pass; inspect evaluation_report.md")
        except Exception as exc:
            errors.append(f"Evaluation failed: {type(exc).__name__}: {exc}")

    for name in ("events.jsonl", "session_summary.json", "resource_samples.csv"):
        path = session_dir / name
        if path.is_file():
            artifacts.append(str(path))
    success = bool(results) and all(r["success"] for r in results) and not errors
    if mode == "full":
        success = success and duration_s >= target and bool(evaluation and evaluation.passed)
    summary = {
        "run_id": collector.session_id,
        "mode": mode,
        "success": success,
        "cancelled": cancelled,
        "duration_s": duration_s,
        "target_duration_s": target,
        "session_dir": str(session_dir),
        "results": results,
        "telemetry": telemetry_summary,
        "evaluation": {"score": evaluation.score, "passed": evaluation.passed} if evaluation else None,
        "artifacts": list(dict.fromkeys(artifacts)),
        "errors": errors,
    }
    session_dir.mkdir(parents=True, exist_ok=True)
    controller_path = session_dir / "controller_summary.json"
    if controller_path.exists():
        raise FileExistsError(f"Controller summary already exists: {controller_path}")
    summary["artifacts"].append(str(controller_path))
    controller_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
