"""Visible Word workflow using the frozen SessionContext/TaskResult API.

Windows desktop Word is required; no COM document generation, clipboard paste,
cloud calls or third-party Python dependencies. The input adapter is provided
by the Behaviour branch. Launch uses visible Win+R -> WINWORD.EXE /w.
Actual process ancestry must be measured; no explorer.exe-parentage claim.

From the repository root:
    python -m modules.office_worker --check
    python -m modules.office_worker --smoke --output-dir C:\\PersonaOS\\outputs\\smoke01

Use --smoke ONLY in the team's authorized Windows VM. The controller, not this
module, owns the full-session timer. Escape/upper-left corner stops input.
"""
from __future__ import annotations

from contextlib import nullcontext
import ctypes
from ctypes import wintypes
from pathlib import Path, PureWindowsPath
import re
import shutil
import sys
import time
from types import SimpleNamespace
import unicodedata
import xml.etree.ElementTree as ET
import zipfile

from core.interfaces import SessionContext, TaskResult


DEFAULT_PARAGRAPHS = (
    "Project overview: This fictional team is preparing a lightweight office workflow "
    "demonstration. The goal is to complete a research and reporting session with "
    "clear outputs, visible editing and a reliable handover.",
    "Progress this week: The team agreed the module interfaces and divided ownership "
    "of office tasks, browser tasks, behaviour and telemetry. Local checks cover "
    "timing variation, error reporting and the expected report filename.",
    "Risks and dependencies: The Windows test environment still needs an observed "
    "integration run. Office availability, focus changes and file transfer readiness "
    "must be checked on the assigned VM. Local tests do not establish GUI success.",
    "Next steps: Run the approved browser research task, review relevant findings "
    "and update this report. Then repeat the office smoke test, inspect the saved "
    "document and collect genuine system and resource evidence for the final demo.",
    "Source note: This report uses fictional local scenario data. It does not claim "
    "that live research or the final fifteen-minute demonstration has already passed.",
)


def _validate_text(value, label, maximum=15000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty text of at most {maximum} characters")
    if any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 or
           0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f"{label} contains unsupported control characters")
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _report(config):
    office = config.get("office", {})
    if not isinstance(office, dict):
        raise ValueError("config['office'] must be an object")
    title = _validate_text(office.get("document_title", "Weekly Project Progress Report"), "document_title", 200)
    paragraphs = office.get("paragraphs", DEFAULT_PARAGRAPHS)
    if not isinstance(paragraphs, (list, tuple)) or not 3 <= len(paragraphs) <= 5:
        raise ValueError("office.paragraphs must contain three to five paragraphs")
    body = [_validate_text(p, "paragraph", 3000) for p in paragraphs]
    if "\n" in title or "\t" in title or any("\n" in p or "\t" in p for p in body):
        raise ValueError("Use single-line title and paragraphs; layout is controlled by the workflow")
    return office, title, body


def _output_path(directory, filename):
    if not isinstance(filename, str) or not filename or len(filename) > 120:
        raise ValueError("document_name must be a filename of at most 120 characters")
    if (PureWindowsPath(filename).name != filename or Path(filename).name != filename or
            re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or
            filename.endswith((" ", ".")) or Path(filename).suffix.lower() != ".docx"):
        raise ValueError("document_name must be a plain .docx filename without folders")
    if filename.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        raise ValueError("document_name uses a reserved Windows device name")
    base = Path(directory).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    stem = Path(filename).stem
    for index in range(1, 10001):
        candidate = base / (filename if index == 1 else f"{stem}_{index:03d}.docx")
        # lexists behaviour: a broken symlink also counts as occupied.
        if not candidate.exists() and not candidate.is_symlink():
            if len(str(candidate)) > 220:
                raise ValueError("Use a shorter output directory (Word Save As compatibility)")
            return candidate
    raise FileExistsError("Too many existing reports; choose a new session output directory")


def find_word():
    """Read-only discovery; no installation, registry modification or downloads."""
    if sys.platform != "win32":
        raise RuntimeError("Office GUI execution requires the assigned Windows VM with desktop Microsoft Word")
    import winreg
    candidates = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WINWORD.EXE", 0,
                                    winreg.KEY_READ | view) as key:
                    candidates.append(winreg.QueryValueEx(key, None)[0].strip('"'))
            except OSError:
                pass
    found = shutil.which("WINWORD.EXE")
    if found:
        candidates.append(found)
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file() and path.name.lower() == "winword.exe":
            return str(path.resolve())
    raise RuntimeError("Desktop Microsoft Word was not found. Ask the VM owner to verify its installation/licence; no editor fallback was selected.")


class WindowsWordDesktop:
    """Bounded native desktop operations; only the launched Word PID is owned.

    Supports desktop Word's OpusApp/_WwG editor and the native F12 Save As dialog.
    The dialog's filename field is found by control IDs, not screen coordinates.
    Unexpected activation/licensing dialogs stop the workflow for operator review.
    """
    def __init__(self, behaviour):
        executable = find_word()
        from behaviour.typing import WindowsKeyboard
        self.executable, self.behaviour = executable, behaviour
        self.keyboard = WindowsKeyboard()
        self.user32 = self.keyboard.user32
        self.process = None
        self.hwnd = None
        self.mode = "editor"
        self.dialog = None
        self.filename_control = None
        self.clock = time.monotonic
        self._bind()

    def _bind(self):
        u = self.user32
        self.enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        signatures = {
            "EnumWindows": ([self.enum_callback, wintypes.LPARAM], wintypes.BOOL),
            "EnumChildWindows": ([wintypes.HWND, self.enum_callback, wintypes.LPARAM], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetClassNameW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetForegroundWindow": ([], wintypes.HWND),
            "GetShellWindow": ([], wintypes.HWND),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowEnabled": ([wintypes.HWND], wintypes.BOOL),
            "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
            "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            "GetDlgCtrlID": ([wintypes.HWND], ctypes.c_int),
            "GetWindowRect": ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(u, name)
            fn.argtypes, fn.restype = args, result

        class GUIINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]
        self.GUIINFO = GUIINFO
        u.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUIINFO)]
        u.GetGUIThreadInfo.restype = wintypes.BOOL
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def _image(self, pid):
        handle = self.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if self.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                return Path(buffer.value).resolve()
            return None
        finally:
            self.kernel32.CloseHandle(handle)

    def _class(self, hwnd):
        buffer = ctypes.create_unicode_buffer(256)
        self.user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def _pid(self, hwnd):
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def _windows(self, parent=None):
        found = []
        @self.enum_callback
        def callback(hwnd, _):
            found.append(hwnd)
            return True
        if parent is None:
            self.user32.EnumWindows(callback, 0)
        else:
            self.user32.EnumChildWindows(parent, callback, 0)
        return found

    def _focus(self):
        info = self.GUIINFO()
        info.cbSize = ctypes.sizeof(info)
        if not self.user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            raise RuntimeError("Cannot inspect foreground keyboard focus")
        return info.hwndFocus

    def check_cancel(self):
        self.keyboard.check_cancel()
        if hasattr(self.behaviour, "check_cancel"):
            self.behaviour.check_cancel()

    def _wait(self, predicate, label, timeout=20):
        deadline = self.clock() + timeout
        # Attempt cap also bounds tests with a stalled/injected clock.
        for _ in range(400):
            self.check_cancel()
            result = predicate()
            if result:
                return result
            if self.clock() >= deadline:
                break
            self.behaviour.pause(action="wait")
        raise TimeoutError(label)

    def _owned_foreground(self):
        front = self.user32.GetForegroundWindow()
        if not self.process or not front or self._pid(front) != self.process.pid:
            raise RuntimeError("Word lost focus; input stopped before typing into another application")
        return front

    def assert_editor(self):
        self.check_cancel()
        if self._owned_foreground() != self.hwnd:
            raise RuntimeError("A Word dialog is covering the editor; dismiss it manually and retry")
        focus = self._focus()
        if not focus or self._class(focus) != "_WwG":
            raise RuntimeError("Word document body is not focused; input stopped")

    def assert_filename(self):
        self.check_cancel()
        if self._owned_foreground() != self.dialog or self._focus() != self.filename_control:
            raise RuntimeError("Save As filename field lost focus; input stopped")

    def open(self):
        self.check_cancel()
        previous = set(self._windows())
        previous_word_pids = {self._pid(h) for h in previous if self._class(h) == "OpusApp"}
        shell = self.user32.GetShellWindow()
        if not shell:
            raise RuntimeError("Windows Explorer desktop is unavailable; Run-dialog launch cannot proceed")
        shell_pid = self._pid(shell)
        self.keyboard.hotkey("win", "r")
        def run_dialog():
            front = self.user32.GetForegroundWindow()
            if (front and front not in previous and self._class(front) == "#32770"
                    and self._pid(front) == shell_pid):
                return front
            return None
        launcher = self._wait(run_dialog, "Windows Run dialog did not open. Close an existing Run dialog or check VM policy.")
        def launcher_focus():
            if (self.user32.GetForegroundWindow() != launcher or
                    self._class(self._focus()) != "Edit" or
                    self.user32.GetAncestor(self._focus(), 2) != launcher):
                raise RuntimeError("Windows Run command field lost focus; launch stopped")
            self.check_cancel()
        launcher_focus()
        self.keyboard.focus_guard = launcher_focus
        self.keyboard.hotkey("ctrl", "a")
        guard = getattr(self.behaviour, "input_guard", None)
        with guard(launcher_focus) if guard else nullcontext():
            self.behaviour.type_text(f'"{self.executable}" /w')
            launcher_focus()
        self.keyboard.press("enter")
        self.keyboard.focus_guard = None
        def window():
            matches = [h for h in self._windows() if h not in previous
                       and self._class(h) == "OpusApp" and self.user32.IsWindowVisible(h)
                       and self._pid(h) not in previous_word_pids
                       and self._image(self._pid(h)) == Path(self.executable).resolve()]
            if len(matches) > 1:
                raise RuntimeError("Multiple new Word windows appeared; ownership is ambiguous")
            return matches[0] if matches else None
        self.hwnd = self._wait(window, "Word editor did not open. Check first-run/licence prompts.", 30)
        self.process = SimpleNamespace(pid=self._pid(self.hwnd))
        self.user32.ShowWindow(self.hwnd, 9)
        self.user32.SetForegroundWindow(self.hwnd)
        self._wait(lambda: self.user32.GetForegroundWindow() == self.hwnd,
                   "Windows did not activate the new Word window. Activate it and rerun.")
        self._wait(lambda: self._class(self._focus()) == "_WwG", "Word opened without document-body focus; check first-run dialogs.")
        self.assert_editor()

    def hotkey(self, *keys):
        self.assert_editor()
        self.keyboard.focus_guard = self.assert_editor
        self.keyboard.hotkey(*keys)

    def save_as(self, path):
        if path.exists() or path.is_symlink():
            raise FileExistsError("Output appeared during the run; refusing to overwrite")
        self.hotkey("f12")
        self.keyboard.focus_guard = None
        def dialog():
            return next((h for h in self._windows() if self._pid(h) == self.process.pid
                         and self._class(h) == "#32770" and self.user32.IsWindowVisible(h)), None)
        self.dialog = self._wait(dialog, "Native Save As dialog did not appear. Check Word's first-run/save settings.")
        self._wait(lambda: self.user32.GetForegroundWindow() == self.dialog,
                   "Save As did not receive focus")
        def filename_edit():
            edits = [h for h in self._windows(self.dialog) if self._class(h) == "Edit"
                     and self.user32.IsWindowVisible(h) and self.user32.IsWindowEnabled(h)
                     and self.user32.GetDlgCtrlID(h) in (1001, 1152)]
            return edits[0] if len(edits) == 1 else None
        self.filename_control = self._wait(filename_edit,
            "Unsupported Save As layout: could not identify exactly one filename edit control")
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(self.filename_control, ctypes.byref(rect)):
            raise RuntimeError("Cannot locate filename control")
        self.keyboard.focus_guard = lambda: self._owned_foreground() == self.dialog or self._wrong_dialog()
        self.keyboard.click((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        self._wait(lambda: self._focus() == self.filename_control, "Cannot focus Save As filename field")
        self.keyboard.focus_guard = self.assert_filename
        self.keyboard.hotkey("ctrl", "a")
        guard = getattr(self.behaviour, "input_guard", None)
        with guard(self.assert_filename) if guard else nullcontext():
            self.assert_filename()
            self.behaviour.type_text(str(path))
            self.assert_filename()
        if path.exists() or path.is_symlink():
            raise FileExistsError("Output appeared during Save As; overwrite was not confirmed")
        self.keyboard.press("enter")
        self.keyboard.focus_guard = None
        self._wait(lambda: not self.user32.IsWindow(self.dialog),
                   "Save As did not finish. Check overwrite/format/permission dialogs manually.", 30)
        self._wait(lambda: self.user32.GetForegroundWindow() == self.hwnd,
                   "Word did not return from Save As")
        self.assert_editor()

    def _wrong_dialog(self):
        raise RuntimeError("Unexpected dialog received focus")

    def verify_saved(self, path, expected):
        last_error = [None]
        def verified():
            self.assert_editor()
            try:
                _verify_docx(path, expected)
                return True
            except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
                last_error[0] = str(exc)
                return False
        try:
            self._wait(verified, "Saved report could not be verified", 15)
        except TimeoutError as exc:
            raise RuntimeError(f"{exc}: {last_error[0]}") from exc

    def close(self):
        self.hotkey("alt", "f4")
        self.keyboard.focus_guard = None
        self._wait(lambda: not self.user32.IsWindow(self.hwnd),
                   "Owned Word window did not close; check its prompt manually", 15)

    def recovery_note(self):
        if self.process is not None:
            return (f"Input stopped. Inspect only the Word instance launched for this run "
                    f"(PID {self.process.pid}); an unsaved document or dialog may remain. "
                    "No unrelated windows were closed and no process was killed.")
        return ("No Word window was adopted. Inspect the Run dialog or any newly opened Word window; "
                "a launch may still be pending. No process was killed.")


def _normalise(text):
    text = unicodedata.normalize("NFC", text).translate(str.maketrans({
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "}))
    return " ".join(text.split())


def _verify_docx(path, expected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Expected a nonempty regular report file")
    with zipfile.ZipFile(path) as archive:
        entry = archive.getinfo("word/document.xml")
        if entry.file_size > 5_000_000:
            raise ValueError("Unexpectedly large report XML")
        root = ET.fromstring(archive.read(entry))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = ["".join(t.text or "" for t in p.findall(".//w:t", ns))
                  for p in root.findall(".//w:body/w:p", ns)]
    if _normalise("\n".join(paragraphs)) != _normalise(expected):
        raise ValueError("Saved Word content does not match the intended report; check AutoCorrect/focus")


def run(context: SessionContext) -> TaskResult:
    """One bounded office task; no independent session timer and no shared changes."""
    actions, artifacts = [], []
    desktop = None
    try:
        office, title, paragraphs = _report(context.config)
        path = _output_path(context.output_dir, office.get("document_name", "Weekly_Project_Report.docx"))
        behaviour = context.behaviour
        desktop = WindowsWordDesktop(behaviour)
        desktop.open()
        actions.append("office_opened")
        behaviour.pause(action="navigate")

        def type_into_document(text):
            guard = getattr(behaviour, "input_guard", None)
            with guard(desktop.assert_editor) if guard else nullcontext():
                desktop.assert_editor()
                behaviour.type_text(text)
                desktop.assert_editor()

        desktop.hotkey("ctrl", "b")
        type_into_document(title)
        desktop.hotkey("ctrl", "b")
        type_into_document("\n\n")
        for index, paragraph in enumerate(paragraphs, 1):
            type_into_document(paragraph + "\n\n")
            actions.append(f"paragraph_{index}_typed")
            behaviour.pause(action="reading")
        type_into_document("Review status: pending")
        actions.append("report_typed")
        behaviour.pause(action="review")
        desktop.hotkey("ctrl", "home")
        behaviour.pause(action="review")
        desktop.hotkey("ctrl", "end")
        desktop.hotkey("ctrl", "shift", "left")
        type_into_document("reviewed")
        actions.append("revision_applied")
        behaviour.pause(action="save")
        desktop.save_as(path)
        expected = title + "\n\n" + "\n\n".join(paragraphs) + "\n\nReview status: reviewed"
        desktop.verify_saved(path, expected)
        artifacts.append(str(path))
        actions.append("report_saved_and_verified")
        behaviour.pause(action="close")
        desktop.close()
        actions.append("office_closed")
        return TaskResult(success=True, actions_completed=actions, artifacts=artifacts, errors=[])
    except (Exception, KeyboardInterrupt) as exc:
        error = f"{type(exc).__name__}: {exc}"
        if desktop is not None:
            error += " " + desktop.recovery_note()
        return TaskResult(success=False, actions_completed=actions, artifacts=artifacts, errors=[error])


def _main():
    import argparse
    from dataclasses import asdict
    from datetime import datetime, timezone
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Read-only Word/Python preflight")
    mode.add_argument("--smoke", action="store_true", help="Run real Word GUI task inside authorized VM")
    parser.add_argument("--config", type=Path, default=Path("config/settings.json"))
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.check:
        try:
            print(json.dumps({"python": sys.version.split()[0], "platform": sys.platform,
                              "word_executable": find_word(),
                              "note": "Installation discovery only; licence and GUI are not yet verified"}, indent=2))
            return 0
        except Exception as exc:
            print(f"Preflight blocked: {exc}")
            return 1
    if sys.platform != "win32":
        print("Live smoke testing is only supported inside the authorized Windows VM.")
        return 1
    from behaviour.interaction import BehaviourEngine
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (args.output_dir or Path("outputs") / f"office_smoke_{stamp}").resolve()
    output.mkdir(parents=True, exist_ok=True)
    event_path = output / f"behaviour_events_{stamp}.jsonl"
    # Stream the full run; the in-memory event buffer is bounded.
    with event_path.open("x", encoding="utf-8") as stream:
        def sink(event):
            stream.write(json.dumps(event) + "\n")
            stream.flush()
        behaviour = BehaviourEngine.from_config(config.get("behaviour", {}), event_sink=sink)
        result = run(SessionContext(config=config, output_dir=output, behaviour=behaviour))
        report = {"result": asdict(result), "timing": behaviour.timing_summary(),
                  "launch_method": "visible Windows Run dialog: WINWORD.EXE /w; measure actual process ancestry",
                  "validation": "Office-only smoke; not the 900-second integrated demonstration"}
    with (output / f"office_result_{stamp}.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(_main())
