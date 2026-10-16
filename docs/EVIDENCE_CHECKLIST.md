# PersonaOS evidence checklist (Hack23)

Owner: Member 4 (`feature/validation`). This list maps the **official scoring categories** to artifacts this repository actually produces. It does not invent metrics, mark unrun VM tests as passed, or treat organizer infrastructure gaps as code success.

Document format in this tree is **Apache OpenOffice Writer `.odt`**, not Microsoft Word `.docx`. Organizers confirmed on 14 October 2026 that Word is not installed on the VM. They have **not** explicitly confirmed that `.odt` satisfies the final grading requirement; treat that as an open grading risk, not as a passed check.

Statuses used below:

| Status | Meaning |
|---|---|
| **Confirmed in code/tests** | Present in `main` and covered by unit tests. Not a substitute for a live VM run. |
| **Not yet collected** | The system can produce this on the VM; no accepted live capture exists yet. |
| **Blocked** | Cannot be collected until organizers or operators finish an external step. Do not mark passed. |
| **Out of scope for the agent** | Must be captured by a human on the VM (screenshots, Event Viewer, Proxmox). |

Do not automatically bypass browser certificate warnings, disable TLS verification, or rewrite Windows logs.

## Score weights (official)

| Category | Weight |
|---|---:|
| Process authenticity | 30% |
| Human entropy and timing | 30% |
| Complexity of action | 20% |
| Resource efficiency | 20% |

---

## 1. Process authenticity — 30%

What the scoring looks for: a visible desktop workflow whose process tree looks like ordinary office use, not a hidden or spoofed chain.

### Evidence this system can produce

| Evidence | How it is produced | Status |
|---|---|---|
| Agent process identity (pid, ppid, optional parent name/exe) | `events.jsonl` `session_start.data.process` when psutil is present | Confirmed in code/tests; live parent names **not yet collected** |
| Browser launched through the visible Win+R Run dialog (`chrome` then `msedge`) | `modules/web_browser.py`; `browser_session.json` records the browser used | Confirmed in code/tests; live VM launch **not yet collected** |
| Writer launched through Win+R (`soffice.exe -writer`) | `modules/office_worker.py`; no UNO, no clipboard paste | Confirmed in code/tests; live Writer GUI **not yet collected** (PR #7: VM GUI pending) |
| Session-bounded Sysmon Event ID 1 (Image, ParentImage, CommandLine) | `python -m telemetry.evaluator --sysmon` (read-only `wevtutil`) | **Blocked**: Sysmon is not preinstalled. Installer is on the organizer FTP; follow the Discord technical guide. Until then, mark Sysmon **unavailable**, not passed. |
| Process Explorer tree during a run | Manual screenshot on the VM | **Not yet collected** (out of scope for the agent) |
| Process Monitor capture of demo processes | Manual, keep it short | **Not yet collected** |
| Human-operated comparison run (same apps, same VM) | Manual; report next to the agent run without altering either | **Not yet collected** |

### What is not evidence

- The evaluator score is a team heuristic, not process authenticity.
- CPU samples of `python.exe` do not prove Writer or Chrome parentage.
- `SYSMON_IMAGES` in `telemetry/evaluator.py` includes `soffice.exe` and `soffice.bin`. Until Sysmon is installed, `--sysmon` output is still **unavailable**, not a pass.

---

## 2. Human entropy and timing — 30%

What the scoring looks for: pauses and typing that vary like a person, over a session of **at least 15 minutes**.

### Evidence this system can produce

| Evidence | How it is produced | Status |
|---|---|---|
| Per-action pauses (`reading`, `navigate`, `scroll`, `review`, `typing`, …) | `events.jsonl` `kind=pause` via `instrument_behaviour` | Confirmed in code/tests |
| Pause coefficient of variation (team threshold CV ≥ 0.40, ≥ 5 pauses) | `evaluation_report.json` check `pause_variability` | Confirmed in code/tests on synthetic timings; **live VM distribution not yet collected** |
| Per-character typing with optional typo/backspace | `events.jsonl` `kind=typing` (`chars`, `words`, `duration_s`; **never the text**) | Confirmed in code/tests |
| Typing speed 15–120 WPM | `evaluation_report.json` check `typing_speed_human` | Confirmed in code/tests; live WPM **not yet collected** |
| Idle-gap bound (no gap > 120 s) | `evaluation_report.json` check `no_long_idle` | Confirmed in code/tests |
| Full session ≥ 900 s (target 960 s) | `controller_summary.json` `mode=full`, `duration_s`; evaluator `minimum_duration` | Confirmed in controller policy; **15-minute live run not yet collected** |
| Smoke session (one cycle, no 900 s rule) | `main.py --mode smoke`; controller relaxes evaluator minimum to 0 | Confirmed in code/tests |

Behaviour timing is recorded by the agent itself. That is useful readiness data; it is **not** kernel-level proof of human input. Keyboard injection uses Windows `SendInput` and is detectable.

---

## 3. Complexity of action — 20%

What the scoring looks for: more than a single click-and-type demo — research plus a real edited document.

### Browser (Chrome / Edge)

| Evidence | How it is produced | Status |
|---|---|---|
| Visit ≥ 2 configured `https://` sites (target 3), random order | `cycle_NNN_browser/browser_session.json` → `sites_visited`, `visits[]` | Confirmed in code/tests |
| Varied scrolling per page | `visits[].scroll_amounts` | Confirmed in code/tests |
| Follow ≥ 1 visible in-page link (`Ctrl+F` on configured link text) | `links_followed`, `link_results[]` | Confirmed in code/tests |
| Close only the window the agent opened | `browser_closed` | Confirmed in code/tests |
| Certificate / TLS failures recorded honestly | `visits[].status=certificate_error`; errors never click through | Confirmed in code. **Live mock-site run is blocked**: certificates expired 9 Oct 2026. Albert (ST Engineering) acknowledged this on **14 Oct 2026**; a colleague is working on it. Do **not** invent successful visits. The suggestion to uninstall the CA and type URLs must not become an automated TLS bypass. |

Configured sites (may be mock hosts on the VM): Coursera, AWS, Samsung, Apple, Dell.

### OpenOffice Writer (`.odt`)

| Evidence | How it is produced | Status |
|---|---|---|
| Visible typing of title (bold) and 3–5 paragraphs | Office module + behaviour `type_text` | Confirmed in code/tests |
| In-document edit (`pending` → `reviewed`) | `actions_completed` includes `revision_applied` | Confirmed in code/tests |
| Save As to `Weekly_Project_Report.odt` (or `_NNN.odt` if the name is taken) | `cycle_NNN_office/*.odt` listed in `TaskResult.artifacts` | Confirmed in code/tests |
| Saved file is an ODT ZIP with readable `content.xml` | Office `_verify_odt`; Member 4 `scripts/verify_artifacts.py` | Confirmed in code/tests |
| Three consecutive save/reopen checks on the VM | Manual / Writer smoke | **Not yet collected** |

Microsoft Word and `.docx` are **not** expected on this VM.

---

## 4. Resource efficiency — 20%

What the scoring looks for: a session that does real work without looking like a busy-loop or an empty idle wait.

### Evidence this system can produce

| Evidence | How it is produced | Status |
|---|---|---|
| Agent process CPU % and RSS (plus children) | `resource_samples.csv`; `session_summary.json` `resources`; `events.jsonl` `kind=resource` | Confirmed in code when psutil is installed. Optional: without psutil, sessions still run and this file is omitted — record as **unavailable**, not passed. PR #8 reports psutil 7.2.2 already installed on VM 3231. |
| System CPU and memory % | Same CSV columns `system_cpu_percent`, `system_memory_percent` | Same as above |
| Duration vs work (no idle padding to 15 minutes) | Controller repeats real browser+office cycles; never sleeps off the clock | Confirmed in code/tests |
| Proxmox Summary CPU/RAM graphs for the VM | Human capture of the hypervisor view (Writer/browser are **not** in the Python sample) | **Not yet collected** |

`resource_samples.csv` is written only when samples exist. A short smoke run can finish before the 5 s sampler; that is a **warning**, not a forged pass.

---

## Session artifacts to retain (do not edit)

After any real run, keep the whole folder `outputs/telemetry/<session_id>/`:

| File | Role |
|---|---|
| `events.jsonl` | Time series of session/module/pause/typing/result events |
| `session_summary.json` | Duration, results, write failures, resource summary |
| `controller_summary.json` | `run_id`, `mode` (`smoke`/`full`), success, errors |
| `evaluation_report.json` / `.md` | Team heuristic score (not the judges' 30/30/20/20 split) |
| `resource_samples.csv` | Present only if sampling ran |
| `cycle_NNN_browser/browser_session.json` | Navigation and link evidence |
| `cycle_NNN_office/*.odt` | Writer document |

Verify with:

```bat
python scripts\verify_artifacts.py outputs\telemetry\<session_id>
```

The verifier exits `0` only when required artifact checks pass. Recorded task failures, evaluator failures, missing `.odt`, and failed browser navigation are **FAIL**. Certificate errors are reported as infrastructure blockers, not as successful browsing.

---

## 15-minute full-run requirement

| Item | Requirement | Status |
|---|---|---|
| Configured minimum | `session.minimum_duration_seconds` = 900 | Confirmed in `config/settings.json` |
| Configured target | `session.target_duration_seconds` = 960 | Confirmed |
| Full mode refusal if minimum &lt; 900 | `core/session_controller.py` | Confirmed in tests |
| Smoke mode | One cycle; 900 s **not** required | Confirmed |
| Live `python main.py --mode full` on VM 3231/3232 | Duration ≥ target, all tasks succeed, evaluation passes | **Not yet collected** |

---

## Organizer / infrastructure log (14 October 2026)

From Albert, ST Engineering, to Hack23:

1. Microsoft Office is not on the VM; Apache OpenOffice is. Implementation uses Writer + `.odt`.
2. Mock-site certificate expiry is a **known issue**; a colleague is working on it.
3. Sysmon is **not** preinstalled. Installer is on the organizer FTP; Discord technical guide has the steps. Until installed and a run is captured, Sysmon evidence is unavailable.
4. Do not automate “uninstall the certificate” or click through browser warnings. TLS verification stays on (`check_vm.py --sites` and the browser module).
5. `.odt` vs final grade: **unconfirmed**. Raise in #queries if still unclear before the judged run.

These are infrastructure or policy items. They are not Member 4 code failures, and they are not passes.
