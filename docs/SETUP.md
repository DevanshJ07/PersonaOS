# PersonaOS setup on the Hack23 Windows VM

Step by step, from your laptop to an evaluated full run. Never put the team password or the OTP secret into code, logs, screenshots, commits or AI prompts. The team password is in the private **#queries** channel.

## 1. Connect to the VPN (GlobalProtect, on your laptop)

1. Install GlobalProtect from the organiser download page.
2. Portal: `vpn.geniuspi.sg` (no `https://`).
3. Sign in with the Hack23 team username. In the password field, type the team password **immediately followed by** the current 6-digit TOTP code, with no space between them.

## 2. Open the VM in Proxmox

1. With the VPN connected, browse to **https://192.168.50.13:8006/**. This is the Proxmox host for Hack21–30.
2. Log in with the team username and the team password only, without the TOTP digits.
3. Open the Hack23 VM (**3231** or **3232**), start it if it is stopped, and open **Console**.
4. Take a snapshot (for example `clean-baseline`) before you install anything.

## 3. Get files from the team SMB share

1. On the VM, open File Explorer and go to `\\172.29.101.2\hack23`. Use the team account and the team password.
2. **Always copy files to the Desktop first.** Don't run installers or extract ZIPs straight from the share.

## 4. Install Python

1. Run the Python installer (3.10–3.13, 64-bit) from the Desktop.
2. Tick **"Add python.exe to PATH"** on the first screen.
3. Open a new Command Prompt and check:
   ```bat
   python --version
   ```
   `check_vm.py` fails below 3.10 and warns on 3.10 (the team target is 3.11+).

## 5. Install packages offline

Packages are installed offline from the team's bundle. Its file names below come from the setup brief, not from this repo, so confirm them on the VM. From the folder that holds the bundle (with its `wheels\` folder and `requirements-vm.txt`), run either:

```bat
python -m pip install --no-index --find-links wheels -r requirements-vm.txt
```

or run `install_offline.bat` from the same bundle.

Required: `pyautogui` and `pygetwindow` (the browser module needs both). psutil is optional; without it, sessions run but `resource_samples.csv` has no CPU or RAM data. python-docx is optional; only `check_vm.py` looks for it. Everything else is standard library. [requirements.txt](../requirements.txt) lists the pinned versions.

## 6. Copy the repo to the VM

Copy the PersonaOS folder from the share to the Desktop (for example `%USERPROFILE%\Desktop\PersonaOS`). Then run every command below from that folder:

```bat
cd %USERPROFILE%\Desktop\PersonaOS
```

Don't edit code on the VM. Make fixes in git and copy again.

## 7. Readiness check

```bat
python scripts\check_vm.py
python scripts\check_vm.py --sites
```

Each item prints an `[OK  ]`, `[WARN]` or `[FAIL]` line. The run ends with `READY: 0 required item(s) failed` or `NOT READY: N required item(s) failed`, and exits with code 1 if any required item failed.

- Required: Windows, Python ≥ 3.10, pyautogui, pygetwindow, a readable screen size (when pyautogui is installed), Edge or Chrome, Microsoft Word, a valid config, a writable `outputs\telemetry`, and ≥ 2 GB free disk.
- Warnings only: psutil, python-docx, screen below 1280x720, Sysmon service, the `--sites` reachability checks.
- `--sites` makes a real HTTPS request to each `browser.websites` entry, with certificate verification left on.

## 8. Smoke run (one browser + office cycle)

Close other windows and leave the mouse and keyboard alone.

```bat
python main.py --mode smoke
```

Expected output:

```
Session: ...\outputs\telemetry\<session_id>
Result: PASS (<n>s)
```

Any problems appear as `Error:` lines. Task errors start with `browser:` or `office:`. To stop a run, hold **Escape** or move the mouse to the **upper-left corner**.

## 9. Full run (≥ 15 minutes)

```bat
python main.py --mode full
```

The browser and office cycles repeat until `session.target_duration_seconds` (960 s) is reached. The run stops early if a task fails, or after 30 cycles. A full run passes only if every task succeeds, the duration ≥ target, and the evaluation passes.

## 10. Evaluate

```bat
python -m telemetry.evaluator
python -m telemetry.evaluator --sysmon
```

With no argument, the evaluator picks the latest session under `telemetry.output_directory` in the config, which is the same folder `main.py` writes to by default. It prints the report and writes `evaluation_report.json` and `evaluation_report.md` into the session folder. It exits `0` only if every check passes. `--sysmon` reads Sysmon Event ID 1 records through `wevtutil` and normally needs an **elevated** Command Prompt. If the log can't be read, the report says why. See [TELEMETRY.md](TELEMETRY.md) for the checks and the evidence checklist.

## Troubleshooting

Errors appear in the `Error:` lines from `main.py`, in `controller_summary.json`, and, for the browser, in `cycle_NNN_browser\browser_session.json`. Browser errors use the form `reason:detail`.

### Setup and controller

| Message | Meaning / fix |
|---|---|
| `PersonaOS setup error: ...` (exit 2) | `config\settings.json` is missing or invalid, or full mode found `minimum_duration_seconds` < 900 or target < minimum. |
| `full mode requires minimum >=900s and target >= minimum` | Fix the `session` durations in the config. |
| `Unknown behaviour settings: [...]` / `Only the office_worker behaviour profile is supported` | The `behaviour` section only accepts `profile`, `enable_variable_pauses`, `enable_typing_corrections`, `correction_probability` and `seed`. |
| `Full run stopped after 30 task cycles before target duration` | 30 cycles finished before the target time. Check the per-cycle durations in `session_summary.json`. |
| `Full run lasted <n>s; target is <t>s` | The run ended early, usually because a task failed first. Read the earlier `Error:` lines. |
| `Evaluation did not pass; inspect evaluation_report.md` | The session ran, but a check failed. See which one in the report. |
| `No telemetry sessions found in ...` (evaluator) | No session folder with `events.jsonl` exists yet. Run `main.py` first, or pass the session path. |

### Browser (`browser: ...`)

| Message | Meaning / fix |
|---|---|
| `adapter_init_failed:<ErrorType>:...` | The pyautogui/pygetwindow adapters could not start. `ModuleNotFoundError` means a package is missing, so redo step 5. Off Windows this always fails. |
| `run_dialog_not_found:chrome` | Win+R did not open a window titled "Run" within 5 s. Suggestion: close any open Run dialog and retry. If it keeps happening, ask in #queries whether VM policy blocks Win+R. |
| `browser_not_found:chrome` then `browser_launch_timeout:chrome` | Windows showed a "cannot find" box for `chrome` (the module dismisses it), so Chrome is probably not installed. The module then tries `msedge`. |
| `browser_launch_failed:no_browser_window` | Neither Chrome nor Edge opened a window. `check_vm.py` should show the Browser item as FAIL. |
| `certificate_error:<url>` | The page title shows a privacy/certificate interstitial. Most likely the organiser root CA is not trusted on the VM (to confirm on the VM). Install it (see [TELEMETRY.md](TELEMETRY.md), VM setup), then rerun `check_vm.py --sites`. |
| `page_load_timeout:<url>` / `page_load_failed:<url>` | The page did not load within 20 s, or showed a "can't be reached" page. Suggestion: run `check_vm.py --sites` to see whether the site is reachable from the VM. |
| `link_not_followed:<site>:no_candidate_worked` | None of the link texts were found on the page. Add `browser.link_texts` for that host in the config. |
| `not_enough_valid_sites:<n>` / `invalid_config_url:<url>` | Fewer than 2 valid `https://` entries in `browser.websites`. |
| `failsafe_triggered:...` | pyautogui fail-safe: the mouse reached a screen corner. The module stops touching the GUI, so close the browser yourself. |
| `browser_window_lost` | The browser window was closed or crashed during the run. |
| `browser_close_failed` | The browser window was still open after Ctrl+Shift+W and Alt+F4. Close it yourself. |

### Office (`office: ...`)

| Message | Meaning / fix |
|---|---|
| `Desktop Microsoft Word was not found...` | WINWORD.EXE is not registered under App Paths and not on PATH. Ask the organisers in #queries. |
| `Office GUI execution requires the assigned Windows VM...` | You ran it somewhere other than Windows. |
| `Windows Run dialog did not open...` | Close any existing Run dialog and retry. |
| `Word editor did not open. Check first-run/licence prompts.` | Suggestion: open Word once by hand, dismiss any first-run, licence or sign-in dialogs, then close it and retry. |
| `Word lost focus...` / `A Word dialog is covering the editor...` / `Word document body is not focused...` | Another window or dialog took focus. Input stopped, so nothing was typed elsewhere. |
| `Saved Word content does not match the intended report; check AutoCorrect/focus` | Word's AutoCorrect changed the text, or a keystroke was lost. Suggestion: try turning off AutoCorrect options in Word (to confirm on the VM which ones matter). |
| `Unsupported Save As layout...` / `Native Save As dialog did not appear...` | The module needs Word's native Save As dialog (a window with one filename edit box). Check Word's first-run and save settings, as the error suggests. Which setting matters is to confirm on the VM. |
| `Release Shift/Ctrl/Alt/Windows keys before automation` | A modifier key was held down when input started. |
| `Windows rejected input; check focus, desktop lock and privilege level` | The desktop was locked, or the target app runs at a different privilege level. Run Python and Word at the same privilege level. |
| `Escape held; desktop automation stopped` / `Cursor in upper-left corner; desktop automation stopped` | Operator cancel. This is expected if you stopped the run on purpose. In the browser it shows as `unexpected_error:CancelledError:...`. |

When the office task stops part-way, its error ends with a recovery note that gives the PID of the Word instance it launched. No other windows are closed and no process is killed.
