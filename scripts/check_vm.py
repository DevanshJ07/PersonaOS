"""PersonaOS Windows VM readiness checklist (Member 4). Standard library only.

    python scripts\\check_vm.py [--config config\\settings.json] [--sites]

Prints one OK / WARN / FAIL line per item and exits 1 if any required item
fails. --sites also tries an HTTPS request to each configured mock site, which
checks reachability and that the organizer root CA is trusted (TLS is verified,
never bypassed). Read-only apart from a temporary file in the telemetry output
directory.
"""

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIN_PYTHON = (3, 10)
RECOMMENDED_PYTHON = (3, 11)
MIN_FREE_GB = 2.0
RECOMMENDED_FREE_GB = 10.0
MIN_SCREEN = (1280, 720)
SITE_TIMEOUT_SECONDS = 10

BROWSERS = {
    "Edge": ("msedge.exe", [r"Microsoft\Edge\Application\msedge.exe"]),
    "Chrome": ("chrome.exe", [r"Google\Chrome\Application\chrome.exe"]),
    "Firefox": ("firefox.exe", [r"Mozilla Firefox\firefox.exe"]),
}
WRITER_RELATIVE = [r"OpenOffice 4\program\soffice.exe"]


class Checklist:
    def __init__(self):
        self.required_failures = 0

    def report(self, status, name, detail, required=True):
        if status == "FAIL" and not required:
            status = "WARN"
        if status == "FAIL":
            self.required_failures += 1
        print(f"[{status:<4}] {name}: {detail}")


def _program_dirs():
    names = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
    return [Path(os.environ[n]) for n in names if os.environ.get(n)]


def _app_path(exe):
    """Look up HKLM/HKCU App Paths registration for an executable."""
    try:
        import winreg
    except ImportError:
        return None
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, None)
        except OSError:
            continue
        path = Path(str(value).strip('"'))
        if path.is_file():
            return path
    return None


def find_executable(exe, relative_paths):
    found = _app_path(exe)
    if found:
        return found
    for base in _program_dirs():
        for rel in relative_paths:
            if (base / rel).is_file():
                return base / rel
    which = shutil.which(exe)
    return Path(which) if which else None


def file_version(path):
    """Windows file version (e.g. 16.0.17928.20114) via version.dll; None elsewhere."""
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None
        # VS_FIXEDFILEINFO: dwSignature, dwStrucVersion, dwFileVersionMS, dwFileVersionLS, ...
        info = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD * 4)).contents
        ms, ls = info[2], info[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def describe(path):
    version = file_version(path)
    return f"{path} (version {version})" if version else str(path)


def package_version(dist):
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def check_platform(c):
    system = platform.system()
    c.report("OK" if system == "Windows" else "FAIL", "Windows",
             f"{platform.platform()} ({platform.machine()})")


def check_python(c):
    v = sys.version_info[:3]
    text = f"{'.'.join(map(str, v))} at {sys.executable}"
    if v < MIN_PYTHON:
        c.report("FAIL", "Python", f"{text}; need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")
    elif v < RECOMMENDED_PYTHON:
        c.report("WARN", "Python", f"{text}; team target is 3.11+")
    else:
        c.report("OK", "Python", text)


def check_packages(c):
    for module, dist, label, required in (
            ("pyautogui", "PyAutoGUI", "pyautogui", True),
            ("pygetwindow", "PyGetWindow", "pygetwindow", True),
            ("psutil", "psutil", "psutil (resource sampling)", False)):
        spec = importlib.util.find_spec(module)
        if spec is None:
            c.report("FAIL", label, "not installed", required=required)
            continue
        version = package_version(dist)
        c.report("OK", label, f"{version or 'unknown version'} at {spec.origin}", required=required)


def check_screen(c):
    if importlib.util.find_spec("pyautogui") is None:
        c.report("WARN", "Screen size", "skipped: pyautogui not installed", required=False)
        return
    try:
        import pyautogui

        width, height = pyautogui.size()
    except Exception as exc:
        c.report("FAIL", "Screen size", f"pyautogui could not read the display: {exc}")
        return
    ok = width >= MIN_SCREEN[0] and height >= MIN_SCREEN[1]
    c.report("OK" if ok else "WARN", "Screen size",
             f"{width}x{height}" + ("" if ok else f" (below {MIN_SCREEN[0]}x{MIN_SCREEN[1]})"), required=False)


def check_browsers(c):
    found = {}
    for name, (exe, rel) in BROWSERS.items():
        path = find_executable(exe, rel)
        if path:
            found[name] = path
    usable = [n for n in ("Edge", "Chrome") if n in found]
    detail = ", ".join(f"{n}: {describe(p)}" for n, p in found.items()) or "none found"
    c.report("OK" if usable else "FAIL", "Browser (Edge/Chrome)", detail)
    if "Firefox" in found and not usable:
        print("       note: the browser module launches chrome or msedge only")


def check_writer(c):
    path = find_executable("soffice.exe", WRITER_RELATIVE)
    c.report("OK" if path else "FAIL", "Apache OpenOffice Writer",
             describe(path) if path else "soffice.exe not found")


def check_sysmon(c):
    if platform.system() != "Windows":
        c.report("WARN", "Sysmon service", "skipped: not Windows", required=False)
        return
    for service in ("Sysmon64", "Sysmon"):
        try:
            out = subprocess.run(["sc", "query", service], capture_output=True, text=True, timeout=10)
        except Exception as exc:
            c.report("WARN", "Sysmon service", f"sc query failed: {exc}", required=False)
            return
        if out.returncode == 0:
            state = "RUNNING" if "RUNNING" in out.stdout else "not running"
            c.report("OK" if state == "RUNNING" else "WARN", "Sysmon service", f"{service} {state}",
                     required=False)
            return
    c.report("WARN", "Sysmon service", "Sysmon/Sysmon64 service not found", required=False)


def load_config(path, c):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        c.report("FAIL", "Config", f"{path}: {exc}")
        return None
    problems = []
    session = config.get("session", {})
    if float(session.get("minimum_duration_seconds", 0)) < 900:
        problems.append("session.minimum_duration_seconds < 900")
    if not config.get("office", {}).get("document_name"):
        problems.append("office.document_name missing")
    sites = config.get("browser", {}).get("websites") or []
    if len(sites) < 2:
        problems.append("browser.websites needs at least 2 entries")
    if any(not str(s).startswith("https://") for s in sites):
        problems.append("browser.websites must all be https://")
    if "output_directory" not in config.get("telemetry", {}):
        problems.append("telemetry.output_directory missing")
    c.report("FAIL" if problems else "OK", "Config", "; ".join(problems) or f"{path} valid")
    return config


def check_output_writable(c, config):
    rel = (config or {}).get("telemetry", {}).get("output_directory", "outputs/telemetry")
    out = Path(rel) if Path(rel).is_absolute() else PROJECT_ROOT / rel
    try:
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=out, prefix=".write_test_", delete=True) as fh:
            fh.write(b"ok")
        c.report("OK", "Telemetry output writable", str(out))
    except OSError as exc:
        c.report("FAIL", "Telemetry output writable", f"{out}: {exc}")
    return out


def check_disk(c, path):
    probe = path if path.exists() else PROJECT_ROOT
    free_gb = shutil.disk_usage(probe).free / 1024 ** 3
    status = "FAIL" if free_gb < MIN_FREE_GB else "WARN" if free_gb < RECOMMENDED_FREE_GB else "OK"
    c.report(status, "Free disk", f"{free_gb:.1f} GB free at {probe.anchor or probe}")


def check_sites(c, config):
    context = ssl.create_default_context()  # Windows ROOT store; verification stays on
    for url in (config or {}).get("browser", {}).get("websites", []):
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "PersonaOS-check"})
        try:
            with urllib.request.urlopen(request, timeout=SITE_TIMEOUT_SECONDS, context=context) as resp:
                c.report("OK", f"Site {url}", f"HTTP {resp.status}", required=False)
        except urllib.error.HTTPError as exc:
            c.report("OK", f"Site {url}", f"reachable, HTTP {exc.code}", required=False)
        except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            hint = " (root CA not trusted?)" if "CERTIFICATE" in str(reason).upper() else ""
            c.report("FAIL", f"Site {url}", f"{reason}{hint}", required=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PersonaOS Windows VM readiness checklist")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "settings.json"))
    parser.add_argument("--sites", action="store_true", help="also test HTTPS reachability of configured sites")
    args = parser.parse_args(argv)

    c = Checklist()
    print("PersonaOS VM readiness")
    check_platform(c)
    check_python(c)
    check_packages(c)
    check_screen(c)
    check_browsers(c)
    check_writer(c)
    check_sysmon(c)
    config = load_config(args.config, c)
    out = check_output_writable(c, config)
    check_disk(c, out)
    if args.sites:
        check_sites(c, config)
    print(f"\n{'NOT READY' if c.required_failures else 'READY'}: {c.required_failures} required item(s) failed")
    return 1 if c.required_failures else 0


if __name__ == "__main__":
    sys.exit(main())
