# PersonaOS

Context-aware synthetic office worker for the NTU x ST Engineering Hackathon 2026 (team Hack23).

PersonaOS runs one visible desktop work session on a Windows VM: it researches a few websites in Chrome or Edge, writes a short *Weekly Project Progress Report* in Microsoft Word, and saves it. Pauses and typing go through a shared behaviour engine that randomizes their timing. A passive telemetry collector logs what the agent did, and an evaluator scores the session afterwards.

**The challenge:** build an agent that behaves like an office worker doing Word tasks and web browsing for **at least 15 minutes**, with **randomized delays**, while the Sysmon and process telemetry it produces still looks normal.

## Architecture

```
main.py ──> core/session_controller.run_session()
              │
              ├─ telemetry.TelemetryCollector      (events.jsonl, summary, CPU/RAM samples)
              ├─ behaviour.BehaviourEngine         (pauses, typing)  ← wrapped by instrument_behaviour()
              │
              ├─ loop: cycle N
              │     ├─ modules.web_browser.run(SessionContext)   → TaskResult
              │     └─ modules.office_worker.run(SessionContext) → TaskResult
              │
              └─ telemetry.evaluator.evaluate_session() + write_reports()
```

Modules share one interface ([core/interfaces.py](core/interfaces.py)). `run(SessionContext) -> TaskResult`, where `SessionContext` holds `config`, `output_dir` and `behaviour`, and `TaskResult` holds `success`, `actions_completed`, `artifacts` and `errors`.

| Package | What it does |
|---|---|
| `core` | `interfaces.py` defines `SessionContext`, `TaskResult` and `BehaviourProtocol`. `session_controller.py` runs the browser and office tasks in cycles, records results and runs the evaluator. |
| `modules/web_browser` | Opens Chrome (Edge as fallback) through the Win+R Run dialog. Visits at least 2 of the configured `https://` sites in random order (target 3), scrolls each page by varied amounts, tries to follow at least one visible in-page link (`browser.minimum_links_per_session`) by finding its text with Ctrl+F, goes back, then closes only the window it opened. Writes `browser_session.json`. Uses pyautogui and pygetwindow. |
| `modules/office_worker` | Starts desktop Word through Win+R (`WINWORD.EXE /w`). Types a bold title and 3–5 paragraphs, changes the last word from "pending" to "reviewed", saves with F12 Save As, checks the saved `.docx` against the intended text, then closes its own Word window. It sends input only while its own Word window has focus. |
| `behaviour` | `BehaviourEngine`: clipped log-normal pauses per action (`reading`, `navigate`, `scroll`, `review`, `typing`…), typing one character at a time with occasional typo-and-backspace corrections, and Windows `SendInput` keyboard input. To stop, hold Escape or move the cursor to the upper-left corner. |
| `telemetry` | `collector.py` writes JSONL events, a session summary and optional psutil CPU/RSS samples of the agent process. `evaluator.py` scores the session with weighted checks and can read Sysmon Event ID 1 records. See [docs/TELEMETRY.md](docs/TELEMETRY.md). |
| `scripts/check_vm.py` | Read-only VM readiness checklist: Windows, Python, packages, screen, Edge/Chrome, Word, Sysmon, config, writable output, disk space. With `--sites`, it also checks HTTPS reachability of the configured sites. |

## How a session runs

As implemented in [core/session_controller.py](core/session_controller.py):

1. `main.py` loads `config/settings.json` and calls `run_session(config, output_dir, mode)`.
2. The target duration is set: **smoke** = 0 s (one cycle); **full** = `session.target_duration_seconds` (960). Full mode refuses to run if `minimum_duration_seconds` < 900 or the target is less than the minimum.
3. A `TelemetryCollector` creates `<output-dir>/<session_id>/` (default `outputs/telemetry/`) (UTC timestamp plus a random suffix) and logs `session_start`.
4. `BehaviourEngine.from_config(config["behaviour"])` is wrapped by `instrument_behaviour()`, so every pause and typing burst is timed and logged. The typed text itself is never logged.
5. **Cycle loop.** Each cycle runs the browser task, then the office task. Each task gets its own folder, `cycle_NNN_browser/` or `cycle_NNN_office/`, and runs inside `collector.module(name)`.
   - The loop stops if a task fails or is cancelled. In smoke mode it stops after one cycle. In full mode it stops once elapsed time ≥ target, or after 30 cycles. The duration is never padded with idle waiting.
6. `collector.end_session()` writes `session_summary.json`, plus `resource_samples.csv` when psutil collected samples.
7. The evaluator runs on the session folder (without Sysmon) and writes `evaluation_report.json` and `.md`. In smoke mode the duration check is relaxed to 0 s.
8. `controller_summary.json` is written. **Smoke** passes if every task succeeded with no errors. **Full** also needs duration ≥ target and an evaluation that passes.
9. `main.py` prints `Session: <dir>` and `Result: PASS|FAIL (<n>s)`, then any `Error:` lines. Exit code: `0` pass, `1` fail, `2` setup error (`PersonaOS setup error: ...`).

## Quick start (on the Windows VM)

Full VM setup (VPN, Proxmox, SMB, offline install) is in [docs/SETUP.md](docs/SETUP.md).

```bat
:: from the offline bundle folder (wheels\ + requirements-vm.txt, see docs\SETUP.md)
python -m pip install --no-index --find-links wheels -r requirements-vm.txt

:: from the repo folder
cd %USERPROFILE%\Desktop\PersonaOS
python scripts\check_vm.py
python scripts\check_vm.py --sites
python main.py --mode smoke
python main.py --mode full
python -m telemetry.evaluator --sysmon
```

`main.py` options (from `python main.py --help`; default paths are relative to the repo folder):

| Flag | Default |
|---|---|
| `--mode {smoke,full}` | `smoke` |
| `--config CONFIG` | `config\settings.json` |
| `--output-dir OUTPUT_DIR` | `outputs\telemetry` |

During a run, leave the VM alone. To stop, hold **Escape** or move the mouse to the **upper-left corner**.

## Outputs

Everything goes under `<output-dir>\<session_id>\` (default `outputs\telemetry\<session_id>\`). A full run has one `cycle_NNN_*` folder per task per cycle.

| File | Contents |
|---|---|
| `events.jsonl` | One JSON event per line (`ts`, `t`, `kind`, `data`): session/module start and end, results, pauses, typing bursts (character and word counts only), resource samples |
| `session_summary.json` | Duration, per-module durations, TaskResults, event count, write failures, CPU/RAM summary |
| `resource_samples.csv` | Agent process CPU %, RSS MB and thread count, plus system CPU and memory % (only when psutil is installed) |
| `cycle_NNN_browser\browser_session.json` | Browser used, `sites_visited`, `links_followed`, each visit with its status and scroll amounts, link attempts, actions and errors |
| `cycle_NNN_office\Weekly_Project_Report.docx` | The saved Word report (`office.document_name`; a `_002` suffix is added instead of overwriting) |
| `evaluation_report.json` / `.md` | Checks, score out of 100, metrics, optional Sysmon observations |
| `controller_summary.json` | Run id, mode, success, duration, per-task results, evaluation score, artifact list, errors |

## Evaluating a run

```bat
python -m telemetry.evaluator
python -m telemetry.evaluator outputs\telemetry\<session_id> --config config\settings.json
python -m telemetry.evaluator --sysmon
```

With no argument, the evaluator picks the latest session under `telemetry.output_directory` from the config (`outputs/telemetry`). `main.py` writes to `--output-dir` instead and doesn't read that config key, so if you change one, change the other or pass the session path. It exits `0` only if every check passes. `--sysmon` reads Sysmon Event ID 1 records for the session window through `wevtutil`, read-only, and usually needs an elevated prompt.

Checks (weights add up to 100): `telemetry_complete` (10), `minimum_duration` ≥ 900 s (15), `modules_succeeded` (15), `browser_links_followed` (10), `office_document_created` (10), `pause_variability` with ≥ 5 pauses and CV ≥ 0.40 (15), `typing_speed_human` 15–120 WPM (10), `no_long_idle` with no gap > 120 s (10), `no_module_errors` (5). Thresholds and metrics are explained in [docs/TELEMETRY.md](docs/TELEMETRY.md). The score is the team's readiness check, not the judges' rubric. During a smoke run the controller relaxes `minimum_duration`, but a standalone `python -m telemetry.evaluator` on a smoke session uses the config's 900 s and will fail that check.

## Running tests

The tests don't need a display, Windows, Word or psutil. `tests/test_web_browser.py` uses **pytest**. The other test files are plain `unittest`.

```bash
python -m pip install pytest
python -m pytest tests
```

Use `python -m pytest`, not bare `pytest`: the `-m` form puts the repo root on `sys.path`. Bare `pytest` fails with `ModuleNotFoundError: No module named 'behaviour'`. Without pytest, you can still run the unittest files with `python -m unittest tests.test_behaviour tests.test_office_worker tests.test_session_controller tests.test_telemetry`.

## Team and branches

| Member | Area | Branch |
|---|---|---|
| Member 1 | Office automation (`modules/office_worker.py`) | `feature/office` |
| Member 2 | Web browser (`modules/web_browser.py`), docs | `feature/browser`, `feature/docs` |
| Member 3 | Behaviour engine (`behaviour/`) | `feature/behaviour` |
| Member 4 | Telemetry, evaluator, VM check (`telemetry/`, `scripts/check_vm.py`) | `feature/telemetry` |
| Lead | Integration (`core/session_controller.py`, `main.py`) | `feature/integration` |

## Ethics and limitations

- We observe and report real telemetry. PersonaOS does not forge, hide or rewrite process lineage, Sysmon records or Windows logs. It doesn't inject into other processes or hook input.
- Browser and Word are started through the visible Run dialog. We don't claim any particular parent process. The real parent → child chain can be observed with `python -m telemetry.evaluator --sysmon` or Process Explorer.
- Keyboard input is ordinary Windows `SendInput`. It is synthetic and detectable, and it does not emulate a physical device.
- The collector records only the agent's own actions, never keystrokes or typed text.
- The evaluator score is a readiness heuristic. Passing it doesn't show that the session can't be told apart from a human.
- Windows only for live runs: it needs desktop Word, Edge or Chrome, and an interactive desktop. The browser follows links by searching for visible link text, so heavily changed sites may need `browser.link_texts` overrides.
