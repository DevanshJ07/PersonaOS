# PersonaOS VM readiness report

Owner: Member 3 (`feature/vm-readiness`). Date: 13 Oct 2026.

This report records the state of the Hack23 Windows VM after setup. Items marked "not checked" were not verified. The preflight is `scripts/check_vm.py` (read-only). Setup steps are in [TELEMETRY.md](TELEMETRY.md#vm-setup-hack23).

## Summary

`check_vm.py` reports **NOT READY** with 1 required failure: Microsoft Word is not installed. The mock sites cannot be used yet because their certificates have expired. Sysmon is not installed. All three blockers have been reported (see [Blockers](#blockers)).

## VM identity

| Item | Value |
|---|---|
| VM | 3231 (`vm-team23-user1`) |
| Proxmox node | `pve-03`, Proxmox VE 9.2.2 |
| Proxmox URL | `https://192.168.50.13:8006/` |
| Access | GlobalProtect VPN, portal `vpn.geniuspi.sg` |
| vCPU | 4 |
| RAM | 8 GB (about 2.1 GB used at idle) |
| Boot disk | 50 GB |

## Snapshots

| Snapshot | Taken | Notes |
|---|---|---|
| clean baseline | 12 Oct 2026 21:32 | Taken before any installs. Status OK. |
| `ready_baseline` | 13 Oct 2026 (time not recorded) | Taken after the setup in this report. |

## Clipboard

- Display hardware is set to `clipboard=vnc`.
- Pasting through noVNC did not work in practice, so all commands were typed by hand.
- Whether the VM was fully shut down and powered on after the setting changed: not checked.

## OS and display

- Windows 11, version 10.0.26100 (AMD64)
- Resolution 1280x800 (confirmed by `check_vm.py` and the offline install script)

## Disk

- 39.2 GB free on `C:` (50 GB boot disk). `check_vm.py` reports this as OK.

## Installed software

| Software | Version | Notes |
|---|---|---|
| Windows 11 | 10.0.26100 | |
| Python | 3.12.10 | Installed with "Add to PATH" |
| Google Chrome | 152.0.7977.83 | First-run prompts dismissed |
| Microsoft Edge | 122.0.2365.106 | |
| Apache OpenOffice | 4.1.16 | |
| Microsoft Word | not installed | Required by `check_vm.py` |
| WordPad | not present | |
| Notepad | present | Version not checked |
| Sysinternals Suite | not checked | Extracted to `C:\Tools` |
| Sysmon | not installed | Service not found |
| Firefox, VS Code, WinSCP, FileZilla, WinRAR | not checked | Installers are on the organiser FTP; whether they are installed on the VM was not checked |

Python packages (installed offline):

| Package | Version |
|---|---|
| pyautogui | 0.9.54 |
| pygetwindow | 0.0.9 |
| psutil | 7.2.2 |
| pymsgbox, pytweening, pyscreeze, mouseinfo, pyperclip, pyrect | not checked (installed as dependencies) |
| python-docx | not installed (optional) |

## Offline install method

1. Python 3.12.10 was chosen from the installers on the organiser FTP (3.12.10, 3.13.15 and 3.14.7 were available). Our wheel bundle covers Python 3.10 to 3.13 (because of Pillow), so 3.14 was ruled out.
2. Packages were installed with `bundle\install_offline.bat`, which runs:
   ```bat
   pip install --no-index --find-links wheels -r requirements-vm.txt
   ```
3. Script output: `OK`, screen `1280x800`.

## Code transfer route

- The team SMB share `\\172.29.101.2\hack23` can be reached only from inside the VM. From laptops over the VPN it fails with error `0x80070043`.
- Organiser staff uploaded our files to the share when we asked:
  - `PersonaOS-main.zip` (GitHub `main` branch)
  - `PersonaOS_offline_wheels.zip`
- The share also holds a `pyautogui_bundle` from the organisers.
- Code was extracted to `%USERPROFILE%\Desktop\PersonaOS-main\PersonaOS-main`.
- SHA-256 of the transferred ZIP: not checked.

Organiser FTP `ftp://172.29.101.2` contents: Python 3.12.10 / 3.13.15 / 3.14.7 installers, `root_ca.crt`, `sysinternalsuite.zip`, and installers for Chrome, Firefox, Apache OpenOffice, VS Code, WinSCP, FileZilla and WinRAR.

## Root CA

- `root_ca.crt` was taken from the organiser FTP and installed to Local Machine > Trusted Root Certification Authorities.
- Windows recognises the chain.

## Mock-site results

| Site | Chrome result |
|---|---|
| `https://www.coursera.org` | `NET::ERR_CERT_DATE_INVALID` |
| `https://aws.amazon.com` | `NET::ERR_CERT_DATE_INVALID` |
| `https://www.samsung.com` | `NET::ERR_CERT_DATE_INVALID` |
| `https://www.apple.com` | `NET::ERR_CERT_DATE_INVALID` |
| `https://www.dell.com` | `NET::ERR_CERT_DATE_INVALID` |

- The `apple.com` leaf certificate was issued by "Prod GeniusPi SWWW CA Intermediate CA" and is valid from 11 Jul 2026 to 9 Oct 2026, so it has expired.
- The VM clock is correct (13 Oct 2026), so this is not a clock problem.
- The warning was not bypassed.
- The other four sites' certificate details: not checked.
- Edge results: not checked.

## check_vm.py results

Command: `python scripts\check_vm.py` (without `--sites`). Result: **NOT READY, 1 required failure.**

| Status | Item |
|---|---|
| OK | Windows |
| OK | Python 3.12.10 |
| OK | pyautogui |
| OK | pygetwindow |
| OK | psutil |
| OK | Screen size 1280x800 |
| OK | Browser (Edge/Chrome) |
| OK | Config valid |
| OK | Telemetry output writable |
| OK | Free disk, 39.2 GB |
| WARN | python-docx not installed (optional) |
| WARN | Sysmon/Sysmon64 service not found |
| FAIL | Microsoft Word not found |

`python scripts\check_vm.py --sites` was not run, because the certificates have expired.

## Sysinternals

- `sysinternalsuite.zip` was extracted to `C:\Tools`.
- Process Explorer (`procexp64`) runs and shows data.
- Process Monitor (`Procmon64`) runs and shows data. The capture was stopped and not saved.
- Sysmon is not installed. `check_vm.py` did not find the service. Reported to the organisers.

## Blockers

| # | Blocker | Impact | Action | Owner / reported to |
|---|---|---|---|---|
| 1 | Microsoft Word not installed | `check_vm.py` FAIL; `modules/office_worker.py` cannot run as written | Proposal: move `office_worker.py` to OpenOffice Writer (4.1.16 is installed) | Member 1 |
| 2 | Mock-site certificates expired (`NET::ERR_CERT_DATE_INVALID` on all five sites) | Browser module cannot visit sites without a certificate warning; `--sites` not run | Reported in #queries | Organisers |
| 3 | Sysmon not installed | No Sysmon Event ID 1 evidence; `telemetry.evaluator --sysmon` will have no data | Reported | Organisers |

## Handoff to Member 4

- The VM was handed to Member 4 for smoke tests after the `ready_baseline` snapshot was taken.
- State at handoff: Python 3.12.10 and the required packages are installed. Code is at `%USERPROFILE%\Desktop\PersonaOS-main\PersonaOS-main`. The root CA is trusted. Process Explorer and Process Monitor are in `C:\Tools`.
- Open items for Member 4:
  - The office worker will fail until blocker 1 is resolved.
  - Browser runs will show certificate warnings until blocker 2 is resolved.
  - Run `python scripts\check_vm.py --sites` once the organisers reissue the certificates.
  - Sysmon evidence depends on blocker 3.
- Smoke test results: not checked (pending, Member 4).
