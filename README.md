# PersonaOS

Context-aware synthetic office worker for the NTU x ST Engineering Hackathon 2026 (team Hack23).

PersonaOS runs one visible desktop work session on a Windows VM: it researches a few websites in Chrome or Edge, writes a short *Weekly Project Progress Report* in **Apache OpenOffice 4.1.16 Writer**, and saves it as `Weekly_Project_Report.odt`. Pauses and typing go through a shared behaviour engine that randomizes their timing. A passive telemetry collector logs what the agent did, and an evaluator scores the session afterwards.

**The challenge:** build an agent that behaves like an office worker doing word-processing tasks and web browsing for **at least 15 minutes**, with **randomized delays**, while the Sysmon and process telemetry it produces still looks normal.

Microsoft Word is **not** installed on the VM (organiser confirmation, 14 Oct 2026), so the office task uses OpenOffice Writer and the OpenDocument `.odt` format. Writer 4.1 can read `.docx` but cannot save it. Whether `.odt` is accepted for grading is still an open question with the organisers (see [docs/EVIDENCE_CHECKLIST.md](docs/EVIDENCE_CHECKLIST.md)).

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

scripts/check_vm.py         before a run: VM readiness checklist
scripts/verify_artifacts.py after a run: read-only check of a session folder
```

Modules share one interface ([core/interfaces.py](core/interfaces.py)). `run(SessionContext) -> TaskResult`, where `SessionContext` holds `config`, `output_dir` and `behaviour`, and `TaskResult` holds `success`, `actions_completed`, `artifacts` and `errors`.

| Package | What it does |
|---|---|
| `core` | `interfaces.py` defines `SessionContext`, `TaskResult` and `BehaviourProtocol`. `session_controller.py` runs the browser and office tasks in cycles, records results and runs the evaluator. |
| `modules/web_browser` | Opens Chrome (Edge as fallback) through the Win+R Run dialog. Visits at least 2 of the configured `https://` sites in random order (target 3), scrolls each page by varied amounts, tries to follow at least one visible in-page link (`browser.minimum_links_per_session`) by finding its text with Ctrl+F, goes back, then closes only the window it opened. Writes `browser_session.json`. Uses pyautogui and pygetwindow. |
| `modules/office_worker` | Finds `soffice.exe` (App Paths registry, PATH, or `Program Files\OpenOffice 4\program\`) and starts it through Win+R as `"<path>\soffice.exe" -writer`. Types a bold title and 3–5 paragraphs, changes the last word from "pending" to "reviewed", saves through Ctrl+S and the **native Windows** Save As dialog, then opens the saved `.odt` (ZIP + `content.xml`) and checks its text against the intended report before closing its own Writer window. It sends input only while its own Writer window (or its Save As filename box) has focus. No UNO, no clipboard paste. Standard library only. |
| `behaviour` | `BehaviourEngine`: clipped log-normal pauses per action (`reading`, `navigate`, `scroll`, `review`, `typing`…), typing one character at a time with occasional typo-and-backspace corrections, and Windows `SendInput` keyboard input. To stop, hold Escape or move the cursor to the upper-left corner. |
| `telemetry` | `collector.py` writes JSONL events, a session summary and optional psutil CPU/RSS samples of the agent process. `evaluator.py` scores the session with weighted checks and can read Sysmon Event ID 1 records. See [docs/TELEMETRY.md](docs/TELEMETRY.md). |
| `scripts/check_vm.py` | Read-only VM readiness checklist: Windows, Python, pyautogui/pygetwindow/psutil, screen, Edge/Chrome, Apache OpenOffice Writer (`soffice.exe`), Sysmon, config, writable output, disk space. With `--sites`, it also checks HTTPS reachability of the configured sites (TLS verified). |
| `scripts/verify_artifacts.py` | Read-only verification of a finished session folder: required files, event and summary consistency, duration, browser navigation, `.odt` report, resource samples, evaluator result. See [Verifying a run](#verifying-a-run). |

## How a session runs

As implemented in [core/session_controller.py](core/session_controller.py):

1. `main.py` loads `config/settings.json` and calls `run_session(config, output_dir, mode)`.
2. The target duration is set: **smoke** = 0 s (one cycle); **full** = `session.target_duration_seconds` (960). Full mode refuses to run if `minimum_duration_seconds` < 900 or the target is less than the minimum.
3. A `TelemetryCollector` creates `<output-dir>/<session_id>/` (default `outputs/telemetry/`; the id is a UTC timestamp plus a random suffix) and logs `session_start`.
4. `BehaviourEngine.from_config(config["behaviour"])` is wrapped by `instrument_behaviour()`, so every pause and typing burst is timed and logged. The typed text itself is never logged.
5. **Cycle loop.** Each cycle runs the browser task, then the office task. Each task gets its own folder, `cycle_NNN_browser/` or `cycle_NNN_office/`, and runs inside `collector.module(name)`.
   - The loop stops if a task fails or is cancelled. In smoke mode it stops after one cycle. In full mode it stops once elapsed time ≥ target, or after 30 cycles. The duration is never padded with idle waiting.
6. `collector.end_session()` writes `session_summary.json`, plus `resource_samples.csv` when psutil collected samples.
7. The evaluator runs on the session folder (without Sysmon) and writes `evaluation_report.json` and `.md`. In smoke mode the duration check is relaxed to 0 s.
8. `controller_summary.json` is written. **Smoke** passes if every task succeeded with no errors. **Full** also needs duration ≥ target and an evaluation that passes.
9. `main.py` prints `Session: <dir>` and `Result: PASS|FAIL (<n>s)`, then any `Error:` lines. Exit code: `0` pass, `1` fail, `2` setup error (`PersonaOS setup error: ...`).

## Quick start (on the Windows VM)

Full VM setup (VPN, Proxmox, file transfer, Python, offline install, root CA, Writer first-run settings) is in [docs/SETUP.md](docs/SETUP.md). Do the Writer one-time setup there before the first office run.

```bat
:: from the repo folder (a fresh folder per code drop, see docs\SETUP.md)
python scripts\check_vm.py
python scripts\check_vm.py --sites

:: single-module smoke tests
python -m modules.office_worker --check
python -m modules.office_worker --smoke --output-dir %USERPROFILE%\Desktop\PersonaOS_outputs\office_smoke01
python -m modules.web_browser

:: integrated runs, then verify the session folder main.py printed
python main.py --mode smoke
python scripts\verify_artifacts.py outputs\telemetry\<session_id>
python main.py --mode full
python scripts\verify_artifacts.py outputs\telemetry\<session_id>
python -m telemetry.evaluator outputs\telemetry\<session_id> --sysmon
```

`main.py` options (from `python main.py --help`; default paths are relative to the repo folder):

| Flag | Default |
|---|---|
| `--mode {smoke,full}` | `smoke` |
| `--config CONFIG` | `config\settings.json` |
| `--output-dir OUTPUT_DIR` | `outputs\telemetry` |

During a run, leave the VM alone. To stop, hold **Escape** or move the mouse to the **upper-left corner**.

### Office module on its own

`python -m modules.office_worker --check` is a read-only preflight. It prints JSON with `python`, `platform`, `writer_executable` (the `soffice.exe` path it found) and a note that the GUI is not yet verified, and exits `0`. If Writer is missing it prints `Preflight blocked: ...` and exits `1`.

`python -m modules.office_worker --smoke [--output-dir DIR] [--config FILE]` runs the real Writer task once (Windows only). Without `--output-dir` it writes to `outputs\office_smoke_<stamp>\`. The folder gets:

- `Weekly_Project_Report.odt` (or `Weekly_Project_Report_002.odt`, `_003`, … if the name is taken; it never overwrites)
- `behaviour_events_<stamp>.jsonl` (pause and typing events)
- `office_result_<stamp>.json` (the `TaskResult`, timing summary and launch method)

It exits `0` only if the report was saved **and** its content verified. This is an office-only check, not the 900-second integrated demonstration.

## Outputs

Everything goes under `<output-dir>\<session_id>\` (default `outputs\telemetry\<session_id>\`). A full run has one `cycle_NNN_*` folder per task per cycle.

| File | Contents |
|---|---|
| `events.jsonl` | One JSON event per line (`ts`, `t`, `kind`, `data`): session/module start and end, results, pauses, typing bursts (character and word counts only), resource samples |
| `session_summary.json` | Duration, per-module durations, TaskResults, event count, write failures, CPU/RAM summary |
| `resource_samples.csv` | Agent process CPU %, RSS MB and thread count, plus system CPU and memory % (only when psutil is installed and at least one 5 s sample was taken) |
| `cycle_NNN_browser\browser_session.json` | Browser used, `sites_visited`, `links_followed`, each visit with its status and scroll amounts, link attempts, actions and errors |
| `cycle_NNN_office\Weekly_Project_Report.odt` | The saved Writer report (`office.document_name`; a `_002` suffix is added instead of overwriting) |
| `evaluation_report.json` / `.md` | Checks, score out of 100, metrics, optional Sysmon observations |
| `controller_summary.json` | Run id, mode, success, duration, per-task results, evaluation score, artifact list, errors |

Keep the whole session folder unedited; it is the evidence. [docs/EVIDENCE_CHECKLIST.md](docs/EVIDENCE_CHECKLIST.md) maps each file to the scoring categories, and VM results are recorded in [docs/VM_TEST_RESULTS.md](docs/VM_TEST_RESULTS.md).

## Evaluating a run

```bat
python -m telemetry.evaluator
python -m telemetry.evaluator outputs\telemetry\<session_id> --config config\settings.json
python -m telemetry.evaluator --sysmon
```

With no argument, the evaluator picks the latest session under `telemetry.output_directory` from the config (`outputs/telemetry`). `main.py` writes to `--output-dir` instead and doesn't read that config key, so if you change one, change the other or pass the session path. It exits `0` only if every check passes. `--sysmon` reads Sysmon Event ID 1 records for the session window through `wevtutil`, read-only, and usually needs an elevated prompt. Sysmon is not preinstalled on the VM (see [docs/SETUP.md](docs/SETUP.md)).

Checks (weights add up to 100): `telemetry_complete` (10), `minimum_duration` ≥ 900 s (15), `modules_succeeded` (15), `browser_links_followed` (10), `office_document_created` (10), `pause_variability` with ≥ 5 pauses and CV ≥ 0.40 (15), `typing_speed_human` 15–120 WPM (10), `no_long_idle` with no gap > 120 s (10), `no_module_errors` (5). Thresholds and metrics are explained in [docs/TELEMETRY.md](docs/TELEMETRY.md). The score is the team's readiness check, not the judges' rubric. During a smoke run the controller relaxes `minimum_duration`, but a standalone `python -m telemetry.evaluator` on a smoke session uses the config's 900 s and will fail that check.

## Verifying a run

```bat
python scripts\verify_artifacts.py outputs\telemetry\<session_id>
python scripts\verify_artifacts.py outputs\telemetry\<session_id> --config config\settings.json
```

[scripts/verify_artifacts.py](scripts/verify_artifacts.py) only reads the session folder; it never writes to it or creates placeholder evidence. It prints one `[PASS]`, `[WARN]` or `[FAIL]` line per check, then `Checks: <n> passed, <n> warned, <n> failed` and `Result: PASS|FAIL`.

| Check | What it looks at |
|---|---|
| `required_files` | `events.jsonl`, `session_summary.json`, `controller_summary.json`, `evaluation_report.json` and `.md` exist and are nonempty |
| `events_jsonl`, `session_summary_json`, `controller_summary_json`, `evaluation_report_json`, `evaluation_report_md` | Each file parses and has its expected fields; `mode` is `smoke` or `full` |
| `session_identifiers` | Folder name, summary `session_id`, controller `run_id` and `session_start` id all match |
| `session_start_end` | Both events are present |
| `timestamps_and_duration` | Timestamps parse, `t` never goes backwards, the durations agree; a **full** run must last ≥ max(900 s, target) |
| `browser_artifacts`, `browser_navigation` | `cycle_*_browser\browser_session.json` parses; ≥ 2 sites visited, ≥ `minimum_links_per_session` links, browser closed, no errors |
| `office_artifacts`, `odt_document` | A nonempty `.odt` named `Weekly_Project_Report.odt` (or `_NNN.odt`) exists, is a ZIP with the OpenDocument text `mimetype` and a readable `content.xml` with a text body. If only a `.docx` is found, the failure says it is the wrong format |
| `resource_metrics` | `resource_samples.csv` columns and numeric values when sampling ran; WARN if psutil was unavailable or a smoke run ended before the first sample |
| `evaluation_present`, `evaluator_result` | The evaluator report has a score, a pass flag and checks, agrees with the controller, and passed |
| `recorded_failures` | No task, module or telemetry write failure was recorded |

Exit code: `0` all required checks passed (warnings allowed), `1` one or more failures, `2` session folder not found. When a browser failure is a certificate error, the reason says it is an organiser infrastructure blocker, not successful browsing.

## Running tests

The tests don't need a display, Windows, Writer or psutil. `tests/test_web_browser.py` uses **pytest**; the other test files are plain `unittest`.

```bash
python -m pip install pytest
python -m pytest tests -q
```

Use `python -m pytest`, not bare `pytest`: the `-m` form puts the repo root on `sys.path`. Bare `pytest` fails with `ModuleNotFoundError: No module named 'behaviour'`. Without pytest, you can still run the unittest files with `python -m unittest tests.test_behaviour tests.test_office_worker tests.test_session_controller tests.test_telemetry tests.test_verify_artifacts`.

## Team and branches

| Member | Area | Branch |
|---|---|---|
| Member 1 | Office automation (`modules/office_worker.py`) | `feature/office` |
| Member 2 | Web browser (`modules/web_browser.py`), docs | `feature/browser`, `feature/docs` |
| Member 3 | Behaviour engine (`behaviour/`) | `feature/behaviour` |
| Member 4 | Telemetry, evaluator, VM check, artifact verifier (`telemetry/`, `scripts/`) | `feature/telemetry`, `feature/validation` |
| Lead | Integration (`core/session_controller.py`, `main.py`) | `feature/integration` |

## Ethics and limitations

- We observe and report real telemetry. PersonaOS does not forge, hide or rewrite process lineage, Sysmon records or Windows logs. It doesn't inject into other processes or hook input.
- The browser and Writer are started through the visible Run dialog. We don't claim any particular parent process. The real parent → child chain can be observed with `python -m telemetry.evaluator --sysmon` (once Sysmon is installed) or Process Explorer.
- TLS is never bypassed. The browser records certificate interstitials as `certificate_error` and does not click through them; `check_vm.py --sites` keeps verification on.
- Keyboard input is ordinary Windows `SendInput`. It is synthetic and detectable, and it does not emulate a physical device.
- The collector records only the agent's own actions, never keystrokes or typed text.
- The evaluator score is a readiness heuristic. Passing it doesn't show that the session can't be told apart from a human.
- The evaluator's Sysmon filter (`SYSMON_IMAGES` in `telemetry/evaluator.py`) does not yet include `soffice.exe` / `soffice.bin`, so `--sysmon` output can leave out Writer even when the log has it. Collect the Writer process chain manually until that is updated.
- Windows only for live runs: it needs Apache OpenOffice Writer, Edge or Chrome, and an interactive desktop. The browser follows links by searching for visible link text, so heavily changed sites may need `browser.link_texts` overrides.
