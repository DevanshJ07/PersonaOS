"""Mocked workflow tests. No Word, Windows, GUI packages or behaviour branch needed."""
import inspect
from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape
import zipfile

from core.interfaces import SessionContext, TaskResult
from modules import office_worker as office


def write_docx_fixture(path, text):
    # Test-only minimal ZIP; not used by the production workflow.
    xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
    xml += ''.join('<w:p><w:r><w:t>' + escape(p) + '</w:t></w:r></w:p>' for p in text.split('\n'))
    xml += '</w:body></w:document>'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', xml)


class FakeBehaviour:
    def __init__(self):
        self.typed = []
        self.pauses = []
        self.desktop = None

    def pause(self, action="default"):
        self.pauses.append(action)

    def type_text(self, text):
        self.typed.append(text)
        if self.desktop.replace_last_word:
            self.desktop.text = self.desktop.text.rsplit(" ", 1)[0] + " "
            self.desktop.replace_last_word = False
        self.desktop.text += text


class FakeDesktop:
    def __init__(self, behaviour):
        behaviour.desktop = self
        self.behaviour = behaviour
        self.text = ""
        self.opened = self.closed = self.replace_last_word = False
        self.focused = True
        self.operations = []

    def open(self):
        self.opened = True
        self.operations.append("open")

    def assert_editor(self):
        if not self.focused:
            raise RuntimeError("focus lost")

    def hotkey(self, *keys):
        self.assert_editor()
        self.operations.append(keys)
        if keys == ("ctrl", "shift", "left"):
            self.replace_last_word = True

    def save_as(self, path):
        self.operations.append("save")
        write_docx_fixture(path, self.text)

    def verify_saved(self, path, expected):
        office._verify_docx(path, expected)
        self.operations.append("verify")

    def close(self):
        self.operations.append("close")
        self.closed = True

    def recovery_note(self):
        return "Owned window may remain; inspect manually."


class OfficeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.behaviour = FakeBehaviour()
        self.context = SessionContext(config={"office": {
            "document_name": "Configured_Report.docx", "document_title": "Configured title"}},
            output_dir=self.directory, behaviour=self.behaviour)
        self.patch = patch.object(office, "WindowsWordDesktop", FakeDesktop)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_frozen_signature_and_structured_success(self):
        self.assertEqual(list(inspect.signature(office.run).parameters), ["context"])
        result = office.run(self.context)
        self.assertIsInstance(result, TaskResult)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.errors, [])
        self.assertEqual(Path(result.artifacts[0]).name, "Configured_Report.docx")
        self.assertTrue(Path(result.artifacts[0]).is_absolute())
        self.assertGreater(Path(result.artifacts[0]).stat().st_size, 0)
        self.assertTrue(self.behaviour.desktop.closed)
        self.assertEqual(self.behaviour.desktop.operations[-3:], ["save", "verify", "close"])

    def test_meaningful_text_revision_and_shared_timing(self):
        result = office.run(self.context)
        self.assertTrue(result.success, result.errors)
        text = self.behaviour.desktop.text
        self.assertIn("Configured title", text)
        self.assertIn("Review status: reviewed", text)
        self.assertNotIn("Review status: pending", text)
        self.assertEqual(len([a for a in result.actions_completed if a.startswith("paragraph_")]), 5)
        for phase in ("navigate", "reading", "review", "save", "close"):
            self.assertIn(phase, self.behaviour.pauses)

    def test_three_consecutive_runs_preserve_existing_outputs(self):
        results = [office.run(self.context) for _ in range(3)]
        self.assertTrue(all(r.success for r in results))
        paths = [r.artifacts[0] for r in results]
        self.assertEqual(len(set(paths)), 3)
        self.assertEqual([Path(p).name for p in paths],
                         ["Configured_Report.docx", "Configured_Report_002.docx", "Configured_Report_003.docx"])

    def test_never_overwrites_existing_report(self):
        path = self.directory / "Configured_Report.docx"
        path.write_bytes(b"existing user file")
        result = office.run(self.context)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(path.read_bytes(), b"existing user file")
        self.assertNotEqual(Path(result.artifacts[0]), path)

    def test_rejects_unsafe_paths_before_desktop_start(self):
        for name in ("../evil.docx", "..\\evil.docx", "C:\\evil.docx", "/tmp/evil.docx",
                     "evil.docx:stream", "CON.docx", "x.txt", "x.docx ", "x\x00.docx"):
            with self.subTest(name=name):
                self.context.config["office"]["document_name"] = name
                result = office.run(self.context)
                self.assertFalse(result.success)
                self.assertEqual(result.actions_completed, [])
                self.assertEqual(self.behaviour.typed, [])

    def test_configured_paragraphs(self):
        self.context.config["office"]["paragraphs"] = ["Progress: complete.", "Risk: schedule.", "Next: review."]
        result = office.run(self.context)
        self.assertTrue(result.success, result.errors)
        self.assertIn("Risk: schedule.", self.behaviour.desktop.text)

    def test_invalid_content_fails_before_open(self):
        for value in ([], ["a"], ["one", "two", "bad\x1b"]):
            self.context.config["office"]["paragraphs"] = value
            result = office.run(self.context)
            self.assertFalse(result.success)
            self.assertEqual(result.actions_completed, [])

    def test_word_unavailable_reports_clear_failure(self):
        with patch.object(office, "WindowsWordDesktop", side_effect=RuntimeError("Word not installed")):
            result = office.run(self.context)
        self.assertFalse(result.success)
        self.assertIn("Word not installed", result.errors[0])
        self.assertEqual(result.artifacts, [])

    def test_focus_loss_stops_before_typing(self):
        class LostFocus(FakeDesktop):
            def open(self):
                super().open()
                self.focused = False
        with patch.object(office, "WindowsWordDesktop", LostFocus):
            result = office.run(self.context)
        self.assertFalse(result.success)
        self.assertEqual(self.behaviour.typed, [])
        self.assertIn("focus lost", result.errors[0])
        self.assertFalse(self.behaviour.desktop.closed)

    def test_incorrect_saved_content_is_not_success(self):
        class BadSave(FakeDesktop):
            def save_as(self, path):
                write_docx_fixture(path, "wrong content")
        with patch.object(office, "WindowsWordDesktop", BadSave):
            result = office.run(self.context)
        self.assertFalse(result.success)
        self.assertIn("does not match", result.errors[0])
        self.assertEqual(result.artifacts, [])
        self.assertFalse(self.behaviour.desktop.closed)

    def test_missing_saved_file_is_not_success(self):
        class MissingSave(FakeDesktop):
            def save_as(self, path):
                pass
        with patch.object(office, "WindowsWordDesktop", MissingSave):
            result = office.run(self.context)
        self.assertFalse(result.success)
        self.assertIn("nonempty", result.errors[0])

    def test_close_failure_keeps_verified_artifact_and_returns_error(self):
        class BadClose(FakeDesktop):
            def close(self):
                raise TimeoutError("close blocked")
        with patch.object(office, "WindowsWordDesktop", BadClose):
            result = office.run(self.context)
        self.assertFalse(result.success)
        self.assertEqual(len(result.artifacts), 1)
        self.assertNotIn("office_closed", result.actions_completed)
        self.assertIn("close blocked", result.errors[0])

    def test_keyboard_interrupt_returns_cancellation_without_more_input(self):
        class Cancelled(FakeDesktop):
            def hotkey(self, *keys):
                raise KeyboardInterrupt("operator stop")
        with patch.object(office, "WindowsWordDesktop", Cancelled):
            result = office.run(self.context)
        self.assertFalse(result.success)
        self.assertEqual(self.behaviour.typed, [])
        self.assertIn("KeyboardInterrupt", result.errors[0])

    def test_permission_error_returns_result(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("access denied")):
            result = office.run(self.context)
        self.assertFalse(result.success)
        self.assertIn("access denied", result.errors[0])

    def test_docx_verification_accepts_word_smart_quotes(self):
        path = self.directory / "smart.docx"
        write_docx_fixture(path, "It’s a “test”.")
        office._verify_docx(path, 'It\'s a "test".')

    def test_output_symlink_does_not_escape_destination(self):
        target = self.directory / "outside.docx"
        target.write_bytes(b"preserve")
        link = self.directory / "Configured_Report.docx"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Symlinks require privilege on this platform")
        result = office.run(self.context)
        self.assertTrue(result.success, result.errors)
        self.assertEqual(target.read_bytes(), b"preserve")
        self.assertNotEqual(Path(result.artifacts[0]), link)

    def test_real_behaviour_engine_with_mock_word_preserves_final_report(self):
        try:
            from behaviour.interaction import BehaviourEngine
        except ImportError:
            self.skipTest("Run after Behaviour is merged, or in the combined review copy")
        class DocumentKeyboard:
            def __init__(self):
                self.desktop = None
            def write(self, text):
                desktop = self.desktop
                if desktop.replace_last_word:
                    desktop.text = desktop.text.rsplit(" ", 1)[0] + " "
                    desktop.replace_last_word = False
                desktop.text += text
            def press(self, key):
                if key == "backspace":
                    self.desktop.text = self.desktop.text[:-1]
                else:
                    self.write({"enter": "\n", "tab": "\t"}[key])
        keyboard = DocumentKeyboard()
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        engine = BehaviourEngine(keyboard=keyboard, sleeper=sleep, clock=lambda: clock[0],
                                 correction_probability=0.25)
        class IntegratedDesktop(FakeDesktop):
            def __init__(self, behaviour):
                super().__init__(behaviour)
                keyboard.desktop = self
        self.context.behaviour = engine
        with patch.object(office, "WindowsWordDesktop", IntegratedDesktop):
            result = office.run(self.context)
        self.assertTrue(result.success, result.errors)
        self.assertTrue(keyboard.desktop.text.endswith("Review status: reviewed"))
        self.assertGreater(sum(e["details"].get("corrections", 0) for e in engine.events), 0)


class NativeDesktopBoundaryTests(unittest.TestCase):
    """Exercise focus/ownership and timeout decisions without loading user32."""
    def desktop(self):
        from types import SimpleNamespace
        d = office.WindowsWordDesktop.__new__(office.WindowsWordDesktop)
        d.process = SimpleNamespace(pid=100)
        d.hwnd = 20
        d.dialog = 30
        d.filename_control = 40
        d.keyboard = SimpleNamespace(check_cancel=lambda: None)
        d.behaviour = SimpleNamespace(pause=lambda action: None)
        d.check_cancel = lambda: None
        d.user32 = SimpleNamespace(GetForegroundWindow=lambda: 20)
        d._pid = lambda hwnd: 100
        d._focus = lambda: 21
        d._class = lambda hwnd: "_WwG"
        return d

    def test_editor_rejects_other_process_and_dialog_and_ribbon(self):
        d = self.desktop()
        d.assert_editor()
        d._pid = lambda hwnd: 999
        with self.assertRaisesRegex(RuntimeError, "lost focus"):
            d.assert_editor()
        d._pid = lambda hwnd: 100
        d.user32.GetForegroundWindow = lambda: 30
        with self.assertRaisesRegex(RuntimeError, "dialog"):
            d.assert_editor()
        d.user32.GetForegroundWindow = lambda: 20
        d._class = lambda hwnd: "Ribbon"
        with self.assertRaisesRegex(RuntimeError, "body"):
            d.assert_editor()

    def test_save_field_focus_is_exact(self):
        d = self.desktop()
        d.user32.GetForegroundWindow = lambda: 30
        d._focus = lambda: 40
        d.assert_filename()
        d._focus = lambda: 41
        with self.assertRaisesRegex(RuntimeError, "filename field"):
            d.assert_filename()

    def test_wait_is_bounded_even_when_clock_stalls(self):
        d = self.desktop()
        d.clock = lambda: 0
        waits = []
        d.behaviour.pause = lambda action: waits.append(action)
        with self.assertRaises(TimeoutError):
            d._wait(lambda: False, "not ready")
        self.assertEqual(len(waits), 400)

    def test_save_rejects_new_collision_before_keyboard_input(self):
        d = self.desktop()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.docx"
            path.write_bytes(b"preserve")
            with self.assertRaises(FileExistsError):
                d.save_as(path)
            self.assertEqual(path.read_bytes(), b"preserve")


if __name__ == "__main__":
    unittest.main()
