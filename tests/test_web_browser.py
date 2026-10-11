"""Tests for modules/web_browser.py using fake GUI, window and clock adapters.

No real display, keyboard or browser is touched. FakeDesktop simulates just
enough of Windows + Chrome (Run dialog, omnibox, find bar, history) for the
workflow to be exercised end to end.
"""

import ast
import inspect
import json
import random
from pathlib import Path

import pytest

from core.interfaces import SessionContext, TaskResult
from modules import web_browser
from modules.web_browser import _run_workflow, _validate_url, run

SITES = [
    "https://www.coursera.org",
    "https://aws.amazon.com",
    "https://www.samsung.com",
    "https://www.apple.com",
    "https://www.dell.com",
]

PAGE_TITLES = {
    "https://www.coursera.org": "Coursera | Online Courses",
    "https://aws.amazon.com": "Cloud Computing Services - Amazon Web Services",
    "https://www.samsung.com": "Samsung Singapore",
    "https://www.apple.com": "Apple",
    "https://www.dell.com": "Dell Technologies",
}

# Link texts that exist on each fake page.
PAGE_LINKS = {
    "https://www.coursera.org": {"Browse"},
    "https://aws.amazon.com": {"Products"},
    "https://www.samsung.com": {"Support"},
    "https://www.apple.com": {"Mac"},
    "https://www.dell.com": {"Laptops"},
}

OTHER_WINDOW = 1  # an unrelated window that must never be closed


class FakeFailSafe(Exception):
    pass


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds

    def timestamp(self):
        return f"t+{self.t:.2f}"


class FakeDesktop:
    """Windows + one browser, driven by the keys the workflow sends."""

    def __init__(self, installed=("chrome",), page_titles=None, page_links=None,
                 refuse_close=False):
        self.windows = {OTHER_WINDOW: "Notes.txt - Notepad"}
        self.active_handle = OTHER_WINDOW
        self.installed = set(installed)
        self.page_titles = dict(PAGE_TITLES if page_titles is None else page_titles)
        self.page_links = dict(PAGE_LINKS if page_links is None else page_links)
        self.refuse_close = refuse_close
        self.next_handle = 100
        self.browser_handle = None
        self.browser_suffix = ""
        self.mode = None
        self.buffer = ""
        self.find_hit = False
        self.history = []
        self.current_url = None
        self.typed_urls = []
        self.launched = []

    # -- helpers -----------------------------------------------------------

    def _new_window(self, title):
        handle = self.next_handle
        self.next_handle += 1
        self.windows[handle] = title
        self.active_handle = handle
        return handle

    def _set_page(self, url, title):
        self.current_url = url
        self.windows[self.browser_handle] = f"{title}{self.browser_suffix}"

    # -- events from FakeGui / FakeBehaviour -------------------------------

    def on_hotkey(self, keys):
        if keys == ("win", "r"):
            self._new_window("Run")
            self.buffer = ""
            self.mode = "run"
        elif keys == ("ctrl", "l") and self.active_handle == self.browser_handle:
            self.mode, self.buffer = "omnibox", ""
        elif keys == ("ctrl", "f") and self.active_handle == self.browser_handle:
            self.mode, self.buffer = "find", ""
        elif keys == ("alt", "left") and self.history:
            url, title = self.history.pop()
            self._set_page(url, title)
        elif keys in (("ctrl", "shift", "w"), ("alt", "f4")):
            if not self.refuse_close:
                self.windows.pop(self.active_handle, None)
                self.active_handle = OTHER_WINDOW

    def on_type(self, text):
        self.buffer += text

    def on_press(self, key):
        if key == "enter":
            self._enter()
        elif key == "esc" and self.mode == "find":
            self.find_hit = self.buffer in self.page_links.get(self.current_url, set())
            self.mode = "page"
        elif key == "esc" and self.windows.get(self.active_handle) in {"chrome", "msedge"}:
            self.windows.pop(self.active_handle)

    def _enter(self):
        if self.mode == "run":
            self.windows.pop(self.active_handle)
            command = self.buffer
            self.launched.append(command)
            if command in self.installed:
                self.browser_suffix = (
                    " - Google Chrome" if command == "chrome" else " - Microsoft​ Edge"
                )
                self.browser_handle = self._new_window("New Tab" + self.browser_suffix)
            else:
                self._new_window(command)  # Windows "cannot find" message box
            self.mode = None
        elif self.mode == "omnibox":
            url = self.buffer
            self.typed_urls.append(url)
            if url in self.page_titles:
                if self.current_url:
                    self.history.append((self.current_url, self.windows[self.browser_handle]
                                         .replace(self.browser_suffix, "")))
                self._set_page(url, self.page_titles[url])
            self.mode = "page"
        elif self.mode == "page" and self.find_hit:
            self.history.append((self.current_url, self.windows[self.browser_handle]
                                 .replace(self.browser_suffix, "")))
            self._set_page(self.current_url + "/linked", f"{self.buffer} page")
            self.find_hit = False


class FakeGui:
    def __init__(self, desktop, fail_on=None):
        self.desktop = desktop
        self.failsafe_exception = FakeFailSafe
        self.fail_on = fail_on
        self.calls = []  # (kind, value, active_handle)
        self.scrolls = []

    def _record(self, kind, value):
        self.calls.append((kind, value, self.desktop.active_handle))
        if self.fail_on == kind:
            raise FakeFailSafe("mouse moved to a screen corner")

    def hotkey(self, *keys):
        self._record("hotkey", keys)
        self.desktop.on_hotkey(keys)

    def press(self, key):
        self._record("press", key)
        self.desktop.on_press(key)

    def scroll(self, amount):
        self._record("scroll", amount)
        self.scrolls.append(amount)

    def move_to(self, x, y, duration=0.4):
        self._record("move_to", (x, y))


class FakeWindows:
    def __init__(self, desktop):
        self.desktop = desktop
        self.activated = []

    def list_windows(self):
        return dict(self.desktop.windows)

    def exists(self, handle):
        return handle in self.desktop.windows

    def title(self, handle):
        return self.desktop.windows.get(handle)

    def active(self):
        return self.desktop.active_handle

    def activate(self, handle):
        self.activated.append(handle)
        if handle in self.desktop.windows:
            self.desktop.active_handle = handle
            return True
        return False

    def center(self, handle):
        return (960, 540) if handle in self.desktop.windows else None


class FakeBehaviour:
    def __init__(self, desktop):
        self.desktop = desktop
        self.pauses = []
        self.typed = []

    def pause(self, action="default"):
        self.pauses.append(action)

    def type_text(self, text):
        self.typed.append(text)
        self.desktop.on_type(text)


def make_config(websites=None, **browser_extra):
    browser = {"websites": list(SITES if websites is None else websites),
               "minimum_links_per_session": 1}
    browser.update(browser_extra)
    return {"browser": browser}


def run_fake(tmp_path, desktop=None, config=None, seed=1, fail_on=None):
    desktop = desktop or FakeDesktop()
    behaviour = FakeBehaviour(desktop)
    gui = FakeGui(desktop, fail_on=fail_on)
    windows = FakeWindows(desktop)
    context = SessionContext(
        config=config or make_config(),
        output_dir=tmp_path / "out",
        behaviour=behaviour,
    )
    result = _run_workflow(context, gui, windows, FakeClock(), random.Random(seed))
    return result, desktop, behaviour, gui, windows


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

def test_run_signature_unchanged():
    sig = inspect.signature(run)
    assert list(sig.parameters) == ["context"]
    assert sig.parameters["context"].annotation is SessionContext
    assert sig.return_annotation is TaskResult


def test_no_forbidden_or_eager_gui_imports():
    tree = ast.parse(Path(web_browser.__file__).read_text(encoding="utf-8-sig"))
    top_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level.add((node.module or "").split(".")[0])
    assert not top_level & {"pyautogui", "pygetwindow"}

    all_imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            all_imports.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            all_imports.add((node.module or "").split(".")[0])
    forbidden = {"selenium", "playwright", "subprocess", "requests", "urllib3",
                 "http", "socket", "webbrowser"}
    assert not all_imports & forbidden
    assert "startfile" not in Path(web_browser.__file__).read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_succeeds(tmp_path):
    result, desktop, behaviour, gui, _ = run_fake(tmp_path)

    assert result.success, result.errors
    assert "browser_launched:chrome" in result.actions_completed
    assert "browser_closed" in result.actions_completed
    visited = [a for a in result.actions_completed if a.startswith("visited:")]
    assert 2 <= len(visited) <= 3
    assert any(a.startswith("link_followed:") for a in result.actions_completed)
    assert any(a.startswith("scrolled:") for a in result.actions_completed)
    assert result.errors == []


def test_browser_launched_through_run_dialog(tmp_path):
    result, desktop, behaviour, gui, _ = run_fake(tmp_path)
    assert gui.calls[0][:2] == ("hotkey", ("win", "r"))
    assert behaviour.typed[0] == "chrome"
    assert desktop.launched == ["chrome"]


def test_edge_fallback_when_chrome_missing(tmp_path):
    desktop = FakeDesktop(installed=("msedge",))
    result, desktop, behaviour, _, _ = run_fake(tmp_path, desktop=desktop)
    assert desktop.launched == ["chrome", "msedge"]
    assert "browser_launched:msedge" in result.actions_completed
    assert any(e.startswith("browser_launch_timeout:chrome") for e in result.errors)
    assert result.success


def test_no_browser_available_returns_clear_error(tmp_path):
    desktop = FakeDesktop(installed=())
    result, *_ = run_fake(tmp_path, desktop=desktop)
    assert not result.success
    assert "browser_launch_failed:no_browser_window" in result.errors
    assert not any(a.startswith("visited:") for a in result.actions_completed)


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

def test_only_configured_sites_are_typed(tmp_path):
    result, desktop, behaviour, _, _ = run_fake(tmp_path)
    assert desktop.typed_urls, "no URLs were typed"
    assert set(desktop.typed_urls) <= set(SITES)
    # URLs reach the browser via behaviour.type_text, not raw key presses.
    for url in desktop.typed_urls:
        assert url in behaviour.typed


def test_validate_url_rejects_unlisted_and_non_https():
    allowed = {"www.apple.com", "aws.amazon.com"}
    assert _validate_url("https://www.apple.com", allowed)
    assert _validate_url("https://aws.amazon.com/products", allowed)
    assert not _validate_url("http://www.apple.com", allowed)
    assert not _validate_url("https://www.google.com", allowed)
    assert not _validate_url("https://evil.apple.com.attacker.net", allowed)
    assert not _validate_url("https://user@www.apple.com", allowed)
    assert not _validate_url("https://www.apple.com:8443", allowed)
    assert not _validate_url("javascript:alert(1)", allowed)


def test_non_https_config_entries_are_never_visited(tmp_path):
    websites = ["http://www.dell.com", "https://www.apple.com", "https://www.coursera.org"]
    result, desktop, *_ = run_fake(tmp_path, config=make_config(websites))
    assert "http://www.dell.com" not in desktop.typed_urls
    assert "invalid_config_url:http://www.dell.com" in result.errors
    assert result.success


def test_visit_order_varies_with_seed(tmp_path):
    orders = set()
    for seed in range(6):
        _, desktop, *_ = run_fake(tmp_path / str(seed), seed=seed)
        orders.add(tuple(desktop.typed_urls))
    assert len(orders) > 1


# ---------------------------------------------------------------------------
# Behaviour usage and scrolling
# ---------------------------------------------------------------------------

def test_behaviour_pauses_used_at_phase_transitions(tmp_path):
    _, _, behaviour, _, _ = run_fake(tmp_path)
    for action in ("navigate", "reading", "scroll", "review", "default"):
        assert action in behaviour.pauses


def test_scroll_amounts_vary_and_are_bounded(tmp_path):
    amounts = []
    for seed in range(5):
        _, _, _, gui, _ = run_fake(tmp_path / str(seed), seed=seed)
        amounts.extend(gui.scrolls)
    assert len(set(amounts)) > 2
    assert all(abs(a) <= web_browser.MAX_SCROLL_ABS for a in amounts)
    assert sum(1 for a in amounts if a < 0) > sum(1 for a in amounts if a > 0)


def test_scroll_plan_includes_occasional_scroll_up():
    rng = random.Random(0)
    plans = [web_browser._scroll_plan(rng, 120) for _ in range(50)]
    flat = [a for plan in plans for a in plan]
    assert any(a > 0 for a in flat)
    assert all(3 <= len(plan) <= 7 for plan in plans)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def test_link_follow_uses_find_bar(tmp_path):
    result, desktop, behaviour, gui, _ = run_fake(tmp_path)
    keys = [c[1] for c in gui.calls]
    assert ("ctrl", "f") in keys
    assert "esc" in keys
    followed = [a for a in result.actions_completed if a.startswith("link_followed:")]
    text = followed[0].split(":")[2]
    assert text in behaviour.typed
    assert ("alt", "left") in keys


def test_link_texts_overridable_from_config(tmp_path):
    links = {url: {"Custom Link"} for url in SITES}
    desktop = FakeDesktop(page_links=links)
    config = make_config(link_texts={host: ["Custom Link"] for host in
                                     ["coursera.org", "aws.amazon.com", "samsung.com",
                                      "apple.com", "dell.com"]})
    result, *_ = run_fake(tmp_path, desktop=desktop, config=config)
    assert result.success
    assert any(a.endswith(":Custom Link") and a.startswith("link_followed:")
               for a in result.actions_completed)


def test_missing_links_record_errors_without_crash(tmp_path):
    desktop = FakeDesktop(page_links={})
    result, desktop, behaviour, *_ = run_fake(tmp_path, desktop=desktop)
    assert not result.success
    assert "browser_closed" in result.actions_completed
    assert any(e.startswith("link_not_followed:") for e in result.errors)
    # It kept trying further sites after the first one failed.
    assert len(desktop.typed_urls) == len(SITES)
    report = json.loads(Path(result.artifacts[0]).read_text(encoding="utf-8"))
    assert report["link_results"]
    assert all(r["result"] == "failed" for r in report["link_results"])


def test_link_found_on_later_site_still_succeeds(tmp_path):
    only_dell = {"https://www.dell.com": {"Support"}}
    desktop = FakeDesktop(page_links=only_dell)
    result, *_ = run_fake(tmp_path, desktop=desktop)
    assert result.success, result.errors
    assert "link_followed:dell.com:Support" in result.actions_completed


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------

def test_page_load_timeout_recorded_and_next_site_tried(tmp_path):
    titles = dict(PAGE_TITLES)
    del titles["https://www.apple.com"]  # navigation produces no title change
    desktop = FakeDesktop(page_titles=titles)
    config = make_config(["https://www.apple.com", "https://www.dell.com",
                          "https://www.coursera.org"])
    result, *_ = run_fake(tmp_path, desktop=desktop, config=config)
    assert "page_load_timeout:https://www.apple.com" in result.errors
    assert "visited:https://www.dell.com" in result.actions_completed
    assert "visited:https://www.coursera.org" in result.actions_completed
    assert result.success


def test_certificate_error_title_skips_site(tmp_path):
    titles = dict(PAGE_TITLES)
    titles["https://www.apple.com"] = "Privacy error"
    # Only three sites, so the cert-error site is always attempted.
    config = make_config(["https://www.apple.com", "https://www.dell.com",
                          "https://www.coursera.org"])
    result, desktop, *_ = run_fake(tmp_path, desktop=FakeDesktop(page_titles=titles),
                                   config=config)
    assert "https://www.apple.com" in desktop.typed_urls
    assert "certificate_error:https://www.apple.com" in result.errors
    assert "visited:https://www.apple.com" not in result.actions_completed
    assert result.success


def test_error_page_titled_with_host_is_treated_as_load_failure(tmp_path):
    titles = dict(PAGE_TITLES)
    titles["https://www.apple.com"] = "www.apple.com"
    config = make_config(["https://www.apple.com", "https://www.dell.com",
                          "https://www.coursera.org"])
    result, *_ = run_fake(tmp_path, desktop=FakeDesktop(page_titles=titles), config=config)
    assert "page_load_failed:https://www.apple.com" in result.errors


def test_failsafe_returns_error_result(tmp_path):
    result, desktop, _, gui, _ = run_fake(tmp_path, fail_on="scroll")
    assert isinstance(result, TaskResult)
    assert not result.success
    assert any(e.startswith("failsafe_triggered") for e in result.errors)
    # After the fail-safe no further GUI input is sent.
    assert gui.calls[-1][0] == "scroll"
    assert result.artifacts


def test_unexpected_exception_reported_not_raised(tmp_path):
    class BrokenGui(FakeGui):
        def move_to(self, x, y, duration=0.4):
            raise RuntimeError("display went away")

    desktop = FakeDesktop()
    behaviour = FakeBehaviour(desktop)
    context = SessionContext(make_config(), tmp_path, behaviour)
    result = _run_workflow(context, BrokenGui(desktop), FakeWindows(desktop),
                           FakeClock(), random.Random(1))
    assert not result.success
    assert any("RuntimeError" in e for e in result.errors)
    assert "browser_closed" in result.actions_completed


def test_time_budget_bounds_the_session(tmp_path):
    titles = {}  # every navigation times out
    config = make_config(time_budget_seconds=30, page_load_timeout_seconds=20)
    result, desktop, *_ = run_fake(tmp_path, desktop=FakeDesktop(page_titles=titles),
                                   config=config)
    assert not result.success
    assert "time_budget_exceeded" in result.errors
    assert len(desktop.typed_urls) < len(SITES)


# ---------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------

def test_close_only_targets_our_window(tmp_path):
    result, desktop, _, gui, _ = run_fake(tmp_path)
    close_calls = [c for c in gui.calls
                   if c[1] in (("ctrl", "shift", "w"), ("alt", "f4"))]
    assert close_calls
    assert all(c[2] == desktop.browser_handle for c in close_calls)
    assert OTHER_WINDOW in desktop.windows
    assert desktop.browser_handle not in desktop.windows


def test_close_failure_means_no_success(tmp_path):
    desktop = FakeDesktop(refuse_close=True)
    result, desktop, _, gui, _ = run_fake(tmp_path, desktop=desktop)
    assert not result.success
    assert "browser_close_failed" in result.errors
    keys = [c[1] for c in gui.calls]
    assert ("ctrl", "shift", "w") in keys and ("alt", "f4") in keys


# ---------------------------------------------------------------------------
# Success logic and report
# ---------------------------------------------------------------------------

def test_success_requires_minimum_links(tmp_path):
    config = make_config(minimum_links_per_session=2)
    result, *_ = run_fake(tmp_path, config=config)
    followed = [a for a in result.actions_completed if a.startswith("link_followed:")]
    assert len(followed) >= 2
    assert result.success


def test_success_requires_two_sites(tmp_path):
    titles = {"https://www.apple.com": "Apple"}
    result, *_ = run_fake(tmp_path, desktop=FakeDesktop(page_titles=titles))
    assert not result.success
    assert sum(a.startswith("visited:") for a in result.actions_completed) == 1


def test_too_few_valid_sites_does_not_launch(tmp_path):
    config = make_config(["https://www.apple.com", "http://www.dell.com"])
    result, desktop, *_ = run_fake(tmp_path, config=config)
    assert not result.success
    assert "not_enough_valid_sites:1" in result.errors
    assert desktop.launched == []


def test_session_report_written_and_listed(tmp_path):
    result, *_ = run_fake(tmp_path)
    assert len(result.artifacts) == 1
    path = Path(result.artifacts[0])
    assert path.is_absolute()
    assert path.name == "browser_session.json"
    assert path.parent == (tmp_path / "out").resolve()
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["success"] is True
    assert report["browser"] == "chrome"
    assert report["sites_visited"] >= 2
    assert report["links_followed"] >= 1
    assert report["browser_closed"] is True
    assert all(v["url"] in SITES for v in report["visits"])
    assert "started_at" in report and "finished_at" in report
    assert report["errors"] == result.errors
