# PersonaOS setup on the Hack23 Windows VM

Step by step, from your laptop to a verified full run. It records what actually worked on VM **3231**. Never put the team password, the OTP secret or any other credential into code, logs, screenshots, commits, this repo or AI prompts. The team password is in the private **#queries** channel.

Microsoft Word is **not** on the VM. The office task uses **Apache OpenOffice 4.1.16 Writer** and saves `Weekly_Project_Report.odt`.

## 1. Connect to the VPN (GlobalProtect, on your laptop)

1. Install GlobalProtect from the organiser download page.
2. Portal: `vpn.geniuspi.sg` (no `https://`).
3. Sign in with the Hack23 team username. In the password field, type the team password **immediately followed by** the current 6-digit TOTP code, with no space between them.

## 2. Open the VM in Proxmox

1. With the VPN connected, browse to **https://192.168.50.13:8006/**. This is the Proxmox host for Hack21–30.
2. Log in with the team username and the team password only, without the TOTP digits.
3. Open the Hack23 VM (**3231** or **3232**), start it if it is stopped, and open **Console**.
4. The VM screen is **1280x800**. `check_vm.py` reports OK at 1280x720 or larger.

### Snapshots on 3231

| Snapshot | Use |
|---|---|
| `clean baseline` | The VM before any of our installs. |
| `ready_baseline` | Restore point for the prepared VM. Roll back here if a run leaves the desktop in a bad state. |

Take a new snapshot before anything risky, and note which snapshot a run started from in [VM_TEST_RESULTS.md](VM_TEST_RESULTS.md).

## 3. Get code and wheels onto the VM

Laptops **cannot** reach the organiser file server `\\172.29.101.2` over the VPN (Windows error `0x80070043`, network name not found). Don't spend time on it. Instead:

1. Post the GitHub download link for the branch to test (for `main`: the repo's `main.zip` link) in **#queries**.
2. Organiser staff upload the code ZIP, and any wheel bundles, to our team SMB folder.
3. On the **VM**, open File Explorer, go to the team SMB folder (`\\172.29.101.2\hack23`) and **copy the ZIP to the Desktop first**. Don't run installers or extract ZIPs straight from the share.
4. Extract each new ZIP into a **new** folder, for example `%USERPROFILE%\Desktop\PersonaOS_2026-10-16\`. **Never extract over an older copy**: stale files left behind would mix two versions, and the old folder's `outputs\` is evidence.

Run every command below from the repo folder you just extracted:

```bat
cd %USERPROFILE%\Desktop\PersonaOS_2026-10-16
```

Don't edit code on the VM. Make fixes in git and send a new ZIP.

## 4. Install Python 3.12.10

1. Get the Python **3.12.10** Windows x64 installer from the organiser FTP, `ftp://172.29.101.2`, and copy it to the Desktop.
2. Run it and tick **"Add python.exe to PATH"** on the first screen.
3. Open a new Command Prompt and check:
   ```bat
   python --version
   ```
   It should print `Python 3.12.10`.

The FTP also has Python 3.13 and 3.14. **Don't use 3.14**: our wheels support Python up to 3.13. `check_vm.py` fails below 3.10, warns on 3.10, and reports OK from 3.11.

## 5. Install packages offline

The VM has no internet access, so packages come from wheel bundles on the SMB folder. On 3231 the offline install worked for:

| Package | Version | Role |
|---|---|---|
| `pyautogui` | 0.9.54 | Required (browser module) |
| `pygetwindow` | 0.0.9 | Required (browser module) |
| `psutil` | 7.2.2 | Optional: CPU/RAM sampling. Without it, sessions run but `resource_samples.csv` is not written |

Their dependencies come from the same bundle. Everything else, including the office module and both scripts, is the Python standard library. [requirements.txt](../requirements.txt) pins the same versions.

Copy the bundle folder to the Desktop, open a Command Prompt in it, and run our bundle's `install_offline.bat` (it installs from the bundle's wheels using `requirements-vm.txt`, with no index). The organiser's own command for their bundle was:

```bat
pip install --no-index --find-links=.\pyautogui_bundle pyautogui
```

`--no-index` stops pip from trying to reach PyPI. If pip reports a missing dependency, ask in #queries for that wheel; don't try to download on the VM.

No other Python packages are needed. In particular, the `.odt` report is checked with the standard library (`zipfile` + `xml`), not with any document library.

## 6. Install the organiser root CA

1. Copy `root_ca.crt` from the organiser FTP to the Desktop and confirm it came from the organisers.
2. Double-click it → **Install Certificate** → **Local Machine** → **Place all certificates in the following store** → **Trusted Root Certification Authorities** → Finish.
3. Install it on the VM only, never on your laptop.

`python scripts\check_vm.py --sites` then tests HTTPS to each configured site with certificate verification on. See [Known organiser issues](#known-organiser-issues) for the current certificate expiry.

## 7. One-time Apache OpenOffice Writer setup

Do this by hand **before the first office run** (and again after restoring a snapshot taken before it):

1. Start **OpenOffice Writer** from the Start menu.
2. Finish the first-run wizard (welcome, user name, registration). The office module stops if it sees a first-run or recovery dialog.
3. In Writer, go to **Tools → Options → OpenOffice → General** and **untick "Use OpenOffice dialogs"**, then OK. `office_worker` needs the **native Windows Save As** dialog; with OpenOffice's own dialogs it fails with `Native Save As did not appear`.
4. Close Writer. Leave no Writer window or document-recovery prompt open before a run.

Check that the module can find Writer:

```bat
python -m modules.office_worker --check
```

It prints JSON with `python`, `platform` and `writer_executable` (the `soffice.exe` under `OpenOffice 4\program\` in Program Files or Program Files (x86)) and exits `0`. `Preflight blocked: ...` means `soffice.exe` was not found. This check does not open the GUI.

## 8. Sysmon and Sysinternals

- **Sysmon is not preinstalled** on the VM. Its installer is on the organiser FTP; follow the organisers' technical guide on Discord. Until it is installed, `check_vm.py` shows a `[WARN] Sysmon service` line and `python -m telemetry.evaluator --sysmon` has no data. Record that as unavailable, not passed.
- **Sysinternals:** Process Explorer and Process Monitor are extracted to `C:\Tools`. Use them for the manual process-tree evidence in [EVIDENCE_CHECKLIST.md](EVIDENCE_CHECKLIST.md). Keep Procmon captures short and filtered so they don't fill the disk.

## 9. Readiness check

```bat
python scripts\check_vm.py
python scripts\check_vm.py --sites
```

Each item prints an `[OK  ]`, `[WARN]` or `[FAIL]` line. The run ends with `READY: 0 required item(s) failed` or `NOT READY: N required item(s) failed`, and exits with code 1 if any required item failed.

- **Required:** Windows, Python ≥ 3.10, pyautogui, pygetwindow, a readable screen size (when pyautogui is installed), Edge or Chrome, **Apache OpenOffice Writer** (`soffice.exe`), a valid config, a writable `outputs\telemetry`, and ≥ 2 GB free disk.
- **Warnings only:** psutil, Python 3.10, screen below 1280x720, Sysmon service, the `--sites` reachability checks.
- `--sites` makes a real HTTPS request to each `browser.websites` entry, with certificate verification left on. A certificate failure adds the hint `(root CA not trusted?)`; with the current expiry, see [Known organiser issues](#known-organiser-issues).

Record the output in [VM_TEST_RESULTS.md](VM_TEST_RESULTS.md) section 1 (redact anything sensitive).

## 10. Module smoke tests

Close other windows and leave the mouse and keyboard alone. To stop, hold **Escape** or move the mouse to the **upper-left corner**.

**Writer** (three consecutive trials are tracked in [VM_TEST_RESULTS.md](VM_TEST_RESULTS.md) section 3):

```bat
python -m modules.office_worker --smoke --output-dir %USERPROFILE%\Desktop\PersonaOS_outputs\writer_trial1
```

The module opens Writer through Win+R (`"...\soffice.exe" -writer`), types the report, saves it through Save As, checks the saved `.odt`, and closes its window. The output folder then holds `Weekly_Project_Report.odt` (or `_002.odt` etc. if the name exists; it never overwrites), `behaviour_events_<stamp>.jsonl` and `office_result_<stamp>.json`, and the exit code is `0` on success. Reopen the `.odt` in Writer by hand and check the title, the body and the last line `Review status: reviewed`.

**Browser:**

```bat
python -m modules.web_browser
```

This writes `outputs\dev_browser\browser_session.json` and prints the `TaskResult`. Don't click through certificate warnings.

## 11. Integrated smoke run (one browser + office cycle)

```bat
python main.py --mode smoke
```

Expected output:

```
Session: ...\outputs\telemetry\<session_id>
Result: PASS (<n>s)
```

Any problems appear as `Error:` lines. Task errors start with `browser:` or `office:`. Then verify the folder `main.py` printed:

```bat
python scripts\verify_artifacts.py outputs\telemetry\<session_id>
```

It is read-only and prints one `[PASS]`/`[WARN]`/`[FAIL]` line per check, ending in `Result: PASS` (exit `0`) or `Result: FAIL` (exit `1`); exit `2` means the folder was not found. On a smoke run, the 900-second rule does not apply, and a missing `resource_samples.csv` is only a warning (the run can end before the first 5 s sample). The checks are listed in the [README](../README.md#verifying-a-run).

## 12. Full run (≥ 15 minutes)

```bat
python main.py --mode full
python scripts\verify_artifacts.py outputs\telemetry\<session_id>
```

The browser and office cycles repeat until `session.target_duration_seconds` (960 s) is reached. The run stops early if a task fails, or after 30 cycles. A full run passes only if every task succeeds, the duration ≥ target, and the evaluation passes. The verifier also fails a full run shorter than 900 s or the target.

## 13. Evaluate and collect evidence

```bat
python -m telemetry.evaluator outputs\telemetry\<session_id>
python -m telemetry.evaluator outputs\telemetry\<session_id> --sysmon
```

With no session argument, the evaluator picks the latest session under `telemetry.output_directory` in the config, which is the same folder `main.py` writes to by default. It writes `evaluation_report.json` and `evaluation_report.md` into the session folder and exits `0` only if every check passes. `--sysmon` needs Sysmon installed (step 8) and an **elevated** Command Prompt; if the log can't be read, the report says why. The filter keeps Writer (`soffice.exe`/`soffice.bin`) as well as browser and Python images. Capture the Writer process chain with Process Explorer as well until Sysmon is available.

Keep the session folder unedited. What to collect for each scoring category is in [EVIDENCE_CHECKLIST.md](EVIDENCE_CHECKLIST.md); record each result (including FAIL and BLOCKED) in [VM_TEST_RESULTS.md](VM_TEST_RESULTS.md). The evaluator's checks are described in [TELEMETRY.md](TELEMETRY.md).

## Known organiser issues

| Issue | What to do |
|---|---|
| Mock-site certificates **expired on 9 Oct 2026**; browsers show `NET::ERR_CERT_DATE_INVALID`. The organisers acknowledged it and are renewing them. | **Never bypass TLS**: don't click through the warning, don't remove or re-add the CA to get past it, and don't disable verification. The browser module records `certificate_error:<url>` and `check_vm.py --sites` fails those sites; mark those tests **BLOCKED** and wait for the renewal. |
| Sysmon not preinstalled | Install from the organiser FTP (step 8). Until then, Sysmon evidence is unavailable. |
| Microsoft Word not on the VM | The office task uses OpenOffice Writer and `.odt`. Whether `.odt` is accepted for grading is still open in #queries. |
| Laptops can't open `\\172.29.101.2` over the VPN (`0x80070043`) | Send the GitHub ZIP link in #queries; organiser staff upload it to our SMB folder (step 3). |

## Troubleshooting

Errors appear in the `Error:` lines from `main.py`, in `controller_summary.json`, and in each task's own files: `cycle_NNN_browser\browser_session.json` for the browser, `office_result_<stamp>.json` for an office `--smoke` run. `verify_artifacts.py` repeats recorded failures under `recorded_failures`.

### Setup and controller

| Message | Meaning / fix |
|---|---|
| `PersonaOS setup error: ...` (exit 2) | `config\settings.json` is missing or invalid, or full mode found `minimum_duration_seconds` < 900 or target < minimum. |
| `full mode requires minimum >=900s and target >= minimum` | Fix the `session` durations in the config. |
| `Unknown behaviour settings: [...]` / `Only the office_worker behaviour profile is supported` | The `behaviour` section only accepts `profile`, `enable_variable_pauses`, `enable_typing_corrections`, `correction_probability` and `seed`. |
| `Session directory already exists: ...` | A session folder with that id already exists. Rerun; never reuse or edit an old session folder. |
| `Full run stopped after 30 task cycles before target duration` | 30 cycles finished before the target time. Check the per-cycle durations in `session_summary.json`. |
| `Full run lasted <n>s; target is <t>s` | The run ended early, usually because a task failed first. Read the earlier `Error:` lines. |
| `Evaluation did not pass; inspect evaluation_report.md` | The session ran, but a check failed. See which one in the report. |
| `No telemetry sessions found in ...` (evaluator) | No session folder with `events.jsonl` exists yet. Run `main.py` first, or pass the session path. |
| `PersonaOS setup error: session directory not found: ...` (verifier, exit 2) | Pass the exact `Session:` folder that `main.py` printed. |

### Browser (`browser: ...`)

Browser errors use the form `reason:detail`.

| Message | Meaning / fix |
|---|---|
| `adapter_init_failed:<ErrorType>:...` | The pyautogui/pygetwindow adapters could not start. `ModuleNotFoundError` means a package is missing, so redo step 5. Off Windows this always fails. |
| `run_dialog_not_found:<chrome or msedge>` | Win+R did not open a window titled "Run" within 5 s. Close any open Run dialog and retry. If it keeps happening, ask in #queries whether VM policy blocks Win+R. |
| `browser_not_found:<command>` then `browser_launch_timeout:<command>` | Windows showed a "cannot find" box for that command (the module dismisses it), so that browser is probably not installed. After `chrome` the module tries `msedge`. A timeout alone means no window appeared within 25 s. |
| `browser_launch_failed:no_browser_window` | Neither Chrome nor Edge opened a window. `check_vm.py` should show the Browser item as FAIL. |
| `certificate_error:<url>` / `certificate_error:link:<site>:<text>` | The page title shows a privacy/certificate interstitial. With the current organiser issue this is the expired mock-site certificate (`NET::ERR_CERT_DATE_INVALID`): mark the run **BLOCKED** and wait for renewal; never bypass it. Otherwise check that the root CA is installed (step 6) and rerun `check_vm.py --sites`. |
| `page_load_timeout:<url>` / `page_load_failed:<url>` | The page title did not change within 20 s, or it showed only the bare host name (a "can't be reached" page). Run `check_vm.py --sites` to see whether the site is reachable from the VM. |
| `focus_lost:<url>:owned=... active=... exists=...` | The browser window the module opened was not in the foreground, so it did not type the URL. Another window took focus; leave the VM alone during runs. |
| `link_not_followed:<site>:no_candidate_worked` | None of the link texts led to a new page. Add `browser.link_texts` for that host in the config. |
| `back_navigation_timeout:<site>` | Alt+Left after following a link did not change the page within 12 s. |
| `time_budget_exceeded` | The browser task used its 360 s budget before visiting enough sites, usually because pages loaded slowly. |
| `not_enough_valid_sites:<n>` / `invalid_config_url:<url>` / `url_rejected:<url>` | Fewer than 2 valid `https://` entries in `browser.websites`, or an entry that is not a plain `https://` URL. |
| `failsafe_triggered:...` | pyautogui fail-safe: the mouse reached a screen corner. The module stops touching the GUI, so close the browser yourself. |
| `browser_window_lost` | The browser window was closed or crashed during the run. |
| `browser_close_failed:owned=... active=... exists=...` / `close_error:...` | The browser window was still open after Ctrl+Shift+W and Alt+F4. Close it yourself. |
| `report_write_failed:...` | `browser_session.json` could not be written. Check that the output folder is writable and the disk has space. |
| `unexpected_error:CancelledError:...` | Operator cancel (Escape or upper-left corner). Expected if you stopped the run on purpose. |

### Office (`office: ...`)

Office errors have the form `office: <ExceptionType>: <message>`. Once input has started, a recovery note follows: either `Input stopped. Inspect only the Writer window launched for this run (PID <n>); an unsaved document or dialog may remain. No unrelated windows were closed and no process was killed.` or, before a Writer window was adopted, `No Writer window was adopted. Inspect the Run dialog or any newly opened Writer window; a launch may still be pending. No process was killed.` Close that Writer window by hand (don't save) before retrying.

| Message | Meaning / fix |
|---|---|
| `Apache OpenOffice Writer (soffice.exe) was not found; ask the VM owner to check its installation.` | `soffice.exe` is not registered under App Paths, not on PATH, and not in `Program Files\OpenOffice 4\program\`. Run `python -m modules.office_worker --check`; ask in #queries if it is missing. |
| `Office GUI execution requires the assigned Windows VM with Apache OpenOffice Writer` | You ran it somewhere other than Windows. |
| `Windows Explorer desktop is unavailable; Run-dialog launch cannot proceed` | No Explorer shell window. Sign in to a normal desktop session in the Console. |
| `TimeoutError: Windows Run dialog did not open. Close an existing Run dialog or check VM policy.` | Close any existing Run dialog and retry. |
| `Windows Run command field lost focus; launch stopped` | Something took focus while the command was typed. Leave the VM alone and retry. |
| `TimeoutError: Writer editor did not open. Check first-run or recovery prompts.` | No new Writer window within 30 s. Usually the first-run wizard or a document-recovery prompt: do step 7 by hand, close Writer, retry. |
| `Writer opened without document-body focus; check first-run dialogs.` / `Windows did not activate the new Writer window. Activate it and rerun.` | Writer opened but a dialog or another window has focus. Dismiss it (step 7) and rerun. |
| `Multiple new Writer windows appeared; ownership is ambiguous` | Close all Writer windows and retry with none open. |
| `Writer lost focus; input stopped before typing into another application` / `A Writer dialog is covering the editor; dismiss it manually and retry` / `Writer document body is not focused; input stopped` | Another window or a Writer dialog took focus. Input stopped, so nothing was typed elsewhere. |
| `TimeoutError: Native Save As did not appear. Disable 'Use OpenOffice dialogs' in Writer options.` | Untick **Tools → Options → OpenOffice → General → Use OpenOffice dialogs** (step 7). |
| `Unsupported Save As layout: could not identify exactly one filename edit control` / `Cannot locate filename control` / `Cannot focus Save As filename field` / `Save As did not receive focus` | The Save As dialog isn't the native Windows one, or it didn't take focus. Check step 7. |
| `Save As filename field lost focus; input stopped` / `Unexpected dialog received focus` | A different window took focus while the path was typed. |
| `TimeoutError: Save As did not finish. Check overwrite/format/permission dialogs manually.` / `Writer did not return from Save As` | A prompt (overwrite, file format, permission) is open in Writer. Look at it, don't accept an overwrite, and use a new output folder. |
| `FileExistsError: Output appeared during the run; refusing to overwrite` / `Output appeared during Save As; overwrite was not confirmed` | Something created the target file mid-run. Use a new, empty output folder. |
| `RuntimeError: Saved report could not be verified: Saved Writer content does not match the intended report; check AutoCorrect/focus` | The saved text differs from what was meant to be typed. Usually Writer's AutoCorrect changed something, or a keystroke was lost. Check **Tools → AutoCorrect Options** (which options matter is to confirm on the VM). |
| `Saved report could not be verified: Expected a nonempty regular report file` / `Saved file is not an ODT document` / `Saved ODT has no text body` / `Unexpectedly large report XML` | The file was not saved as a normal `.odt` within 15 s. Check the Save As file type and that the save finished. |
| `TimeoutError: Owned Writer window did not close; check its prompt manually` | Writer asked something on close. Answer it by hand; the report is already saved and verified. |
| `document_name must be a plain .odt filename without folders` / `... at most 120 characters` / `... reserved Windows device name` | Fix `office.document_name` in the config (default `Weekly_Project_Report.odt`). |
| `Use a shorter output directory (Writer Save As compatibility)` / `Too many existing reports; choose a new session output directory` | Use a shorter, fresh `--output-dir`. |
| `office.paragraphs must contain three to five paragraphs` / `Use single-line title and paragraphs; ...` / `... must be nonempty text ...` / `... contains unsupported control characters` | Fix the `office` section of the config. |
| `Release Shift/Ctrl/Alt/Windows keys before automation` | A modifier key was held down when input started. |
| `Windows rejected input; check focus, desktop lock and privilege level` | The desktop was locked, or Writer runs at a different privilege level. Run Python and Writer at the same privilege level. |
| `Escape held; desktop automation stopped` / `Cursor in upper-left corner; desktop automation stopped` | Operator cancel. Expected if you stopped the run on purpose. |

No other windows are closed and no process is killed by the office module.
