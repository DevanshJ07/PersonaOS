# PersonaOS VM test results (Hack23)

VM identity: **Hack23** (Proxmox guests **3231** / **3232**).  
Branch under test: `feature/validation` (must include latest `main`).  
Document owner: Member 4.

**Rule:** do not mark a test passed unless it was executed on the assigned Windows VM and the artifacts still exist. Empty rows stay `NOT RUN`. Failed runs stay `FAIL` with the real error. Blocked runs stay `BLOCKED` and name the organizer issue. Never paste VPN passwords, TOTP secrets, or other credentials here.

Statuses: `NOT RUN` | `PASS` | `FAIL` | `BLOCKED` | `WARN`

---

## Environment (fill in during the VM session)

| Field | Value |
|---|---|
| Date (UTC) | |
| Operator | Member 4 |
| VM ID / hostname | |
| Snapshot used | PR #8 noted `clean baseline` and `ready_baseline` on 3231 — confirm which one |
| Git commit (`git rev-parse HEAD`) | |
| Python version | PR #8: 3.12.10 on 3231 (unverified in this document) |
| Packages | PR #8: pyautogui 0.9.54, pygetwindow 0.0.9, psutil 7.2.2 (unverified here) |
| Browser available | Chrome / Edge / unknown |
| Writer | Apache OpenOffice 4.1.16 reported on 3231; Word **not** installed |
| Sysmon | **Not preinstalled** (organizer, 14 Oct 2026). Installer: organizer FTP + Discord technical guide |
| Root CA / mock sites | Certificates expired 9 Oct 2026; Albert acknowledged 14 Oct 2026 |

---

## 1. VM preflight

Command:

```bat
python scripts\check_vm.py
python scripts\check_vm.py --sites
```

| Item | Status | Detail / evidence path |
|---|---|---|
| Windows | NOT RUN | |
| Python ≥ 3.10 | NOT RUN | |
| pyautogui / pygetwindow | NOT RUN | |
| psutil | NOT RUN | optional; WARN if missing |
| Screen size | NOT RUN | |
| Browser (Edge or Chrome) | NOT RUN | |
| Apache OpenOffice Writer (`soffice.exe`) | NOT RUN | required; Word is **not** the check |
| Sysmon service | NOT RUN | expected WARN/unavailable until FTP install |
| Config valid | NOT RUN | |
| `outputs\telemetry` writable | NOT RUN | |
| Free disk | NOT RUN | |
| `--sites` HTTPS (TLS verified, never bypassed) | NOT RUN | expect **BLOCKED** until mock-site certs are renewed |

Overall preflight: **NOT RUN**

Notes:

-

---

## 2. Browser smoke test

Do not claim sites were visited if the run died on certificate interstitials. Do not click through privacy errors. Do not disable TLS.

```bat
python -m modules.web_browser
:: or a one-cycle integrated smoke (section 5) and inspect cycle_001_browser\
```

| Check | Status | Evidence |
|---|---|---|
| Run dialog launch (Chrome or Edge) | NOT RUN | |
| ≥ 2 sites `status=visited` | NOT RUN | `browser_session.json` |
| Scroll amounts recorded | NOT RUN | |
| ≥ 1 link followed | NOT RUN | |
| Browser window closed | NOT RUN | |
| Certificate errors (if any) | NOT RUN | record URLs and titles; do not convert to PASS |

`browser_session.json` path:

-

Overall browser smoke: **NOT RUN**

---

## 3. OpenOffice Writer — three consecutive save/reopen tests

Word/DOCX is not in scope. Each trial must save a **new** `.odt` (the worker adds `_002`, `_003`, … instead of overwriting).

```bat
python -m modules.office_worker --check
python -m modules.office_worker --smoke --output-dir %USERPROFILE%\Desktop\PersonaOS\outputs\writer_trial1
python -m modules.office_worker --smoke --output-dir %USERPROFILE%\Desktop\PersonaOS\outputs\writer_trial2
python -m modules.office_worker --smoke --output-dir %USERPROFILE%\Desktop\PersonaOS\outputs\writer_trial3
```

After each save, reopen the `.odt` in Writer by hand and confirm the title, body, and “Review status: reviewed”.

| Trial | Launch Writer | Typed + edited | Saved `.odt` | Reopened OK | Path | Status |
|---|---|---|---|---|---|---|
| 1 | NOT RUN | NOT RUN | NOT RUN | NOT RUN | | NOT RUN |
| 2 | NOT RUN | NOT RUN | NOT RUN | NOT RUN | | NOT RUN |
| 3 | NOT RUN | NOT RUN | NOT RUN | NOT RUN | | NOT RUN |

ODT ZIP/`content.xml` check (offline, after copying files off the VM if needed):

```bat
python scripts\verify_artifacts.py <integrated_session_dir>
```

Overall Writer trials: **NOT RUN**

---

## 4. Telemetry checks

Requires a session folder from a real run (smoke or full). Do not generate fake `events.jsonl`.

| Check | Status | Evidence |
|---|---|---|
| `events.jsonl` present, parses, has `session_start` and `session_end` | NOT RUN | |
| Pause events with varied `duration_s` | NOT RUN | |
| Typing events (`chars`/`words` only; no keystroke text) | NOT RUN | |
| `session_summary.json` duration and results | NOT RUN | |
| `resource_samples.csv` if psutil sampled | NOT RUN | missing + sampling disabled → N/A; missing + psutil present on a full run → FAIL |
| `evaluation_report.json` / `.md` written | NOT RUN | |
| `python -m telemetry.evaluator --sysmon` | NOT RUN | **BLOCKED** until Sysmon is installed; then run from an elevated prompt. Unavailable ≠ PASS. |

Overall telemetry: **NOT RUN**

---

## 5. Integrated smoke test

```bat
python main.py --mode smoke
python scripts\verify_artifacts.py outputs\telemetry\<session_id>
```

One browser cycle and one office cycle. **900 seconds is not required.**

| Check | Status | Evidence |
|---|---|---|
| `main.py` printed `Result: PASS` or `FAIL` | NOT RUN | |
| `controller_summary.json` `mode=smoke` | NOT RUN | |
| `cycle_001_browser\browser_session.json` | NOT RUN | |
| `cycle_001_office\*.odt` | NOT RUN | |
| Artifact verifier exit 0 | NOT RUN | |

Session directory:

-

Overall integrated smoke: **NOT RUN**

---

## 6. Full-duration test (≥ 15 minutes)

```bat
python main.py --mode full
python scripts\verify_artifacts.py outputs\telemetry\<session_id>
python -m telemetry.evaluator outputs\telemetry\<session_id> --sysmon
```

Pass only if every task succeeded, `duration_s` ≥ 960 (configured target), and evaluation passed. The controller does not idle-pad.

| Check | Status | Evidence |
|---|---|---|
| Start / end UTC times | NOT RUN | |
| `duration_s` ≥ 960 | NOT RUN | `controller_summary.json` |
| Repeated browser+office cycles | NOT RUN | `cycle_NNN_*` folders |
| All task `success=true` | NOT RUN | |
| Evaluation passed | NOT RUN | `evaluation_report.md` |
| Artifact verifier exit 0 | NOT RUN | |
| Proxmox CPU/RAM graph covering the run | NOT RUN | screenshot path (out of repo if credentials-adjacent) |

Session directory:

-

Overall full-duration test: **NOT RUN**

---

## 7. Artifact verification

| Session | Mode | Command | Verifier result | Status |
|---|---|---|---|---|
| *(none yet)* | | `python scripts\verify_artifacts.py <dir>` | | NOT RUN |

The verifier must not be pointed at edited or hand-made “pretty” folders. If a check FAILs because the browser recorded `certificate_error`, copy that reason here; do not re-run with TLS disabled to force a PASS.

---

## 8. Outstanding organizer issues (14 October 2026)

Confirmed by **Albert**, ST Engineering:

| Issue | Impact on tests | Status |
|---|---|---|
| Microsoft Office not on the VM; OpenOffice provided | Writer + `.odt` is the implementation. Word/DOCX tests are not applicable. | Acknowledged |
| Mock-site certificates expired 9 Oct 2026 | Browser visits and `check_vm.py --sites` can fail with `NET::ERR_CERT_DATE_INVALID` / `certificate_error`. Albert said a colleague is working on it. | **Open blocker** |
| Sysmon not preinstalled (workshop had implied it would be) | `--sysmon` evidence unavailable. Installer on organizer FTP; Discord technical guide. | **Open blocker** |
| Suggestion to uninstall the CA and type URLs manually | Operators may type URLs for a **human** comparison only. PersonaOS must **not** auto-bypass cert warnings or weaken TLS. | Policy: do not automate |
| `.odt` vs judged deliverable | Not explicitly confirmed as accepted for grading. | **Open question** for #queries |

None of the rows above is a Member 4 code PASS.

---

## Sign-off (after real runs exist)

| Role | Name | Date | Notes |
|---|---|---|---|
| Member 4 | | | |
| VM operator (if different) | | | |

Copy of this file after a judged run should sit next to the session folder. Do not commit credentials, Sysmon XML dumps of unrelated processes, or screenshots that show the VPN password.
