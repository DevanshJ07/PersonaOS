# PersonaOS telemetry, evaluation and VM setup

Owner: Member 4 (`feature/telemetry`). Everything here uses the Python standard library. psutil is optional: without it, sessions still record events, just without CPU/RAM samples.

The collector records only what the agent itself does. It never reads keystrokes or typed text, never changes Windows logs and never touches process ancestry. The evaluator reports measurements from those records. Its score is a team readiness check, not the judges' rubric, and passing it does not show the run is indistinguishable from a human.

## Files produced

Each session writes to `outputs/telemetry/<session_id>/`. The session id is the UTC start time plus a random suffix, e.g. `20261011T100000Z-3fa2`.

| File | Written by | Contents |
|---|---|---|
| `events.jsonl` | collector, live | One JSON object per line: `ts` (UTC ISO), `t` (seconds since start), `kind`, `data` |
| `session_summary.json` | `end_session()` | Duration, per-module durations, TaskResults, event count, write failures, CPU/RAM summary |
| `resource_samples.csv` | `end_session()` | CPU %, RSS MB and thread count of the agent process (+ children), plus system CPU and memory % (needs psutil) |
| `evaluation_report.json` / `.md` | evaluator | Checks, score out of 100, metrics, optional Sysmon observations |

Event kinds:

| Kind | Fields |
|---|---|
| `session_start` | `host`, `os`, `python`, `process` (pid/ppid, and parent name when psutil is present), `resource_sampling` |
| `module_start` / `module_end` / `module_error` | `module`; plus `duration_s` and `ok` on end; `error_type` and `error` on error |
| `result` | The module's TaskResult fields |
| `action` | `name`, `module`, any keyword data |
| `pause` | `action`, `duration_s`, `module` |
| `typing` | `chars`, `words`, `duration_s`, `module` (the text itself is never logged) |
| `behaviour_call` | Any other behaviour-engine method: `method`, `duration_s` |
| `resource` | One CPU/RSS sample |
| `session_end` | `duration_s` |

Telemetry never raises during a session. A failed write is counted in `write_failures`, and any unserialisable value is written as `repr()`.

## Integrator wiring (`core/session_controller.py`)

No changes are needed in the office, browser or behaviour modules. The collector wraps the behaviour engine and the controller wraps each module call:

```python
from pathlib import Path

from core.interfaces import SessionContext, TaskResult
from behaviour.interaction import BehaviourEngine
from modules import office_worker, web_browser
from telemetry import TelemetryCollector, instrument_behaviour
from telemetry.evaluator import evaluate_session, write_reports

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_session(config: dict) -> None:
    collector = TelemetryCollector.from_config(config, PROJECT_ROOT)
    collector.start_session(config["session"]["scenario"])

    # Wrap the shared engine once; modules see a normal BehaviourProtocol object.
    behaviour = instrument_behaviour(BehaviourEngine(), collector)
    output_dir = PROJECT_ROOT / "outputs" / collector.session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    context = SessionContext(config=config, output_dir=output_dir, behaviour=behaviour)

    phases = [("web_browser", web_browser.run), ("office_worker", office_worker.run)]
    try:
        for name, run in phases:  # repeat/extend phases until target_duration_seconds
            try:
                with collector.module(name):  # logs module_start/end, and module_error then re-raises
                    result = run(context)
            except Exception as exc:
                result = TaskResult(success=False, errors=[f"{type(exc).__name__}: {exc}"])
            collector.record_result(name, result)
            collector.action("phase_complete", module=name, success=result.success)
    finally:
        collector.end_session()  # idempotent; always writes session_summary.json

    evaluation = evaluate_session(collector.session_dir, config)
    json_report, md_report = write_reports(evaluation)
    print(f"Evaluation {evaluation.score}/100 -> {md_report}")
```

Requirements on other modules (for the integrator to pass on):

- **Office (Member 1):** `TaskResult.artifacts` must contain the absolute path of the saved `.odt`. Its file name must equal `office.document_name` (or end with `_<document_name>`). Otherwise `office_document_created` fails.
- **Browser (Member 2):** already lists `browser_session.json` (with `links_followed` and `sites_visited`) in `TaskResult.artifacts`. If that path moves, the evaluator also looks in the session folder, its parent and its grandparent, plus their immediate subfolders.
- **Behaviour (Member 3):** nothing to do. The wrapper times `pause(action)` and `type_text(text)` and forwards every other attribute.

## Evaluation checks

Thresholds are constants at the top of `telemetry/evaluator.py`. A session passes only if every check passes.

| Check | Weight | Passes when |
|---|---:|---|
| `telemetry_complete` | 10 | `session_start` and `session_end` are present, the summary was written, and write failures = 0 |
| `minimum_duration` | 15 | Duration ≥ `session.minimum_duration_seconds` (900) |
| `modules_succeeded` | 15 | At least one result was recorded and every result has `success=True` |
| `browser_links_followed` | 10 | `browser_session.json` was found and `links_followed` ≥ `browser.minimum_links_per_session` |
| `office_document_created` | 10 | An artifact named `office.document_name` exists and is not empty |
| `pause_variability` | 15 | At least 5 pauses, and their coefficient of variation is ≥ 0.40 (Workshop 2 heuristic) |
| `typing_speed_human` | 10 | 15–120 WPM, counting 5 characters as one word |
| `no_long_idle` | 10 | No gap longer than 120 s between activity events. Pauses count as idle; typing and other behaviour calls count as activity. |
| `no_module_errors` | 5 | At least one module ran and no `module_error` events were logged |

Metrics reported:

- duration, event count, malformed lines
- pause count, mean, stdev, min, max and CV, overall and per pause action
- typed characters and WPM
- longest idle gap
- agent CPU mean and peak, and peak memory
- browser sites visited and links followed
- path of the office document

Malformed lines in `events.jsonl`, including a truncated last line after a crash, are skipped and counted, not treated as fatal.

## Commands

```bat
:: unit tests (no GUI, no psutil needed)
python -m unittest tests\test_telemetry.py -v

:: VM readiness; exit 1 if any required item fails. --sites also tests HTTPS to the mock sites.
python scripts\check_vm.py
python scripts\check_vm.py --sites

:: evaluate the latest session (exit 0 = pass, 1 = fail)
python -m telemetry.evaluator
python -m telemetry.evaluator outputs\telemetry\<session_id> --config config\settings.json

:: also read Sysmon Event ID 1 for the session window (run from an elevated prompt)
python -m telemetry.evaluator --sysmon
```

`--sysmon` uses `wevtutil qe` read-only. It keeps only Writer (`soffice.exe`/`soffice.bin`), Excel, Outlook, browser and Python images within ±60 s of the session, capped at 500 events. If the log can't be read (not elevated, or Sysmon missing), the report says why and contains no values; collect the evidence manually instead (see the checklist below).

## VM setup (Hack23)

Never put the VPN, Proxmox or SMB password, or the OTP secret, into code, logs, screenshots or AI prompts.

1. **GlobalProtect (laptop):** install it from the organiser download page, then use portal `vpn.geniuspi.sg` (without `https://`). Sign in with the team username, then the password immediately followed by the current 6-digit TOTP code.
2. **Proxmox:** with the VPN connected, open `https://192.168.50.13:8006/` (Hack21–30). Log in with the team username and the base password, without the TOTP digits. Find the Hack23 Windows VM, start it, and open **Console**.
3. **Snapshot:** take a `clean-baseline` snapshot before installing anything, and another before risky runs. For clipboard over VNC, set Hardware → `clipboard=vnc`, then shut down and power on (a reboot is not enough).
4. **Transfers:** in File Explorer on the VM, open the team SMB share `\\172.29.101.2\hack23` (team account, base password). Organiser tools are at `ftp://172.29.101.2`. **Always copy installers and ZIPs to the Desktop (or `C:\PersonaOS\`) first**, then extract or install from there. Never upload private code or credentials to the shared FTP.
5. **Python:** run the staged Python 3.11+ installer from the Desktop. Tick "Add python.exe to PATH".
6. **Offline packages:** copy the staged wheel folder to `C:\PersonaOS\wheels\`, then:
   ```bat
   cd C:\PersonaOS
   py -3.11 -m venv .venv
   .venv\Scripts\activate
   pip install --no-index --find-links C:\PersonaOS\wheels -r requirements.txt
   pip install --no-index --find-links C:\PersonaOS\wheels psutil
   ```
7. **Root CA:** get `root_ca.crt` from the organiser FTP, confirm it came from the organisers, and import it into Local Machine → Trusted Root Certification Authorities on the VM only. Then run `python scripts\check_vm.py --sites`.
8. **Readiness:** run `python scripts\check_vm.py`. Report missing software in #queries, and paste the output into the PR (redacted).
9. **Release and full run (lead):** the lead deploys the tagged release ZIP (`certutil -hashfile release.zip SHA256`) and runs the end-to-end trial: a smoke run, then at least 960 s. Member 4 arranges exclusive VM time with the lead, then runs `python -m telemetry.evaluator --sysmon` and collects the evidence below. Never hot-edit code on the VM.

### Packages to request from the organisers (#queries, staged in team SMB)

- Python 3.11+ Windows x64 installer
- `pyautogui` and its dependencies as Windows wheels: `pymsgbox`, `pytweening`, `pyscreeze`, `mouseinfo`, `pyperclip`, `pyrect`, `pillow`
- `pygetwindow` (and `pyrect`)
- `psutil` (win_amd64 wheel matching the Python version)
- Optional: Sysinternals Suite, if Process Explorer/Monitor are not already on the VM. The saved `.odt` is verified with the standard library (`zipfile` + `xml`), not `python-docx`.

### Evidence checklist (manual, real VM, redact credentials)

- [ ] `check_vm.py` output: installed software and versions, missing items
- [ ] Snapshot name and release SHA-256
- [ ] Mock-site test (`--sites`) after installing the root CA
- [ ] SMB transfer verified
- [ ] Event Viewer → Applications and Services Logs → Microsoft → Windows → Sysmon → Operational, filtered on Event ID 1: `Image`, `ParentImage`, `CommandLine`, `ParentCommandLine`, `UtcTime` for the browser, Writer (`soffice.exe`/`soffice.bin`) and Python
- [ ] Process Explorer tree screenshot during the run
- [ ] Process Monitor capture filtered to the demo processes (keep it short, so it doesn't fill the disk)
- [ ] Proxmox Summary CPU/RAM graphs covering the run
- [ ] `evaluation_report.md` from the full run
- [ ] A human-operated comparison run (browser and Writer launched by hand) with its process chain, reported next to the agent run without changing either. Human timing data belongs to Member 3's baseline work; this item covers only the process evidence.
