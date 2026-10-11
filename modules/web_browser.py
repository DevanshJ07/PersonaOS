"""Browser workflow for PersonaOS (Member 2).

One bounded session: launch Chrome (Edge fallback) through the Windows Run
dialog, visit a few configured sites, read and scroll, follow at least one
visible in-page link, close only the window we opened, and return a
TaskResult. The session controller owns overall duration.

All keyboard, mouse and window access goes through small adapters so the
workflow can be tested without a display. pyautogui/pygetwindow are imported
lazily inside the real adapters only.
"""

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from core.interfaces import SessionContext, TaskResult

# Candidate visible link texts per site (matched on host without "www.").
# Overridable per host via config["browser"]["link_texts"].
DEFAULT_LINK_TEXTS = {
    "coursera.org": ["Browse", "Explore"],
    "apple.com": ["Mac", "iPad", "Support"],
    "dell.com": ["Laptops", "Support"],
    "samsung.com": ["Mobile", "Support"],
    "aws.amazon.com": ["Products", "Pricing"],
}

# Browsers tried in order through the Run dialog.
BROWSER_COMMANDS = ["chrome", "msedge"]

# Lower-case title fragments that indicate a TLS/certificate interstitial.
CERT_ERROR_MARKERS = (
    "privacy error",
    "connection is not private",
    "connection isn't private",
    "certificate error",
    "ssl error",
)

DEFAULTS = {
    "time_budget_seconds": 360,
    "target_sites_per_session": 3,
    "launch_timeout_seconds": 25,
    "run_dialog_timeout_seconds": 5,
    "page_load_timeout_seconds": 20,
    "link_load_timeout_seconds": 12,
    "close_timeout_seconds": 8,
    "scroll_unit": 120,  # one mouse-wheel notch on Windows
    "treat_host_title_as_error": True,
    "run_dialog_title": "Run",
}

POLL_SECONDS = 0.25
SETTLE_SECONDS = 1.0
MIN_SITES = 2
MAX_SCROLL_ABS = 1200

_WINDOW_GONE = object()


class BrowserWindowLost(Exception):
    """Raised when the browser window we opened disappears unexpectedly."""


# ---------------------------------------------------------------------------
# Real adapters (lazy imports)
# ---------------------------------------------------------------------------

class _PyAutoGuiAdapter:
    """Keyboard, mouse and scroll calls via pyautogui."""

    def __init__(self):
        import pyautogui

        pyautogui.FAILSAFE = True
        self._pg = pyautogui
        self.failsafe_exception = pyautogui.FailSafeException

    def hotkey(self, *keys):
        self._pg.hotkey(*keys)

    def press(self, key):
        self._pg.press(key)

    def scroll(self, amount):
        self._pg.scroll(amount)

    def move_to(self, x, y, duration=0.4):
        self._pg.moveTo(x, y, duration=duration)


class _PyGetWindowAdapter:
    """Window enumeration and focus via pygetwindow (Windows only)."""

    def __init__(self):
        import ctypes

        import pygetwindow

        self._gw = pygetwindow
        self._user32 = ctypes.windll.user32

    def _window(self, handle):
        return self._gw.Win32Window(handle)

    def list_windows(self):
        # _hWnd is pygetwindow's stable native handle for a window.
        return {w._hWnd: w.title for w in self._gw.getAllWindows() if w.title}

    def exists(self, handle):
        return bool(self._user32.IsWindow(handle))

    def title(self, handle):
        if not self.exists(handle):
            return None
        return self._window(handle).title

    def active(self):
        window = self._gw.getActiveWindow()
        return window._hWnd if window is not None else None

    def activate(self, handle):
        if not self.exists(handle):
            return False
        window = self._window(handle)
        if window.isMinimized:
            window.restore()
        try:
            window.activate()
        except self._gw.PyGetWindowException:
            # pygetwindow raises spuriously ("error code 0") even on success;
            # the active-window check below is the real verification.
            pass
        return self.active() == handle

    def center(self, handle):
        if not self.exists(handle):
            return None
        w = self._window(handle)
        if w.width <= 0 or w.height <= 0:
            return None
        return (w.left + w.width // 2, w.top + w.height // 2)


class _RealClock:
    def now(self):
        import time

        return time.monotonic()

    def sleep(self, seconds):
        import time

        time.sleep(seconds)

    def timestamp(self):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(context: SessionContext) -> TaskResult:
    """Execute the browser workflow with real GUI adapters."""
    try:
        gui = _PyAutoGuiAdapter()
        windows = _PyGetWindowAdapter()
    except Exception as exc:  # missing display, non-Windows, import failure
        return TaskResult(
            success=False,
            errors=[f"adapter_init_failed:{type(exc).__name__}:{exc}"],
        )
    return _run_workflow(context, gui, windows, _RealClock())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _normalize_title(title):
    # Edge puts a zero-width space in "Microsoft​ Edge".
    return (title or "").replace("​", "").strip().lower()


def _browser_kind(title):
    t = _normalize_title(title)
    if t.endswith("google chrome"):
        return "chrome"
    if t.endswith("microsoft edge"):
        return "msedge"
    return None


def _is_cert_error(title):
    t = _normalize_title(title)
    return any(marker in t for marker in CERT_ERROR_MARKERS)


def _site_key(host):
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _title_is_bare_host(title, host):
    # Chrome/Edge error pages ("This site can't be reached") use the host as title.
    t = _normalize_title(title)
    for name in {host.lower(), _site_key(host)}:
        if t == name or t.startswith(name + " - "):
            return True
    return False


def _allowed_hosts(websites):
    """Return (hosts, errors) for the https entries in the configured list."""
    hosts, errors = set(), []
    for url in websites:
        parts = urlsplit(str(url))
        if parts.scheme == "https" and parts.hostname and "@" not in parts.netloc:
            hosts.add(parts.hostname.lower())
        else:
            errors.append(f"invalid_config_url:{url}")
    return hosts, errors


def _validate_url(url, allowed_hosts):
    """Only https URLs whose host is in the configured list may be typed."""
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and "@" not in parts.netloc
        and port in (None, 443)
        and (parts.hostname or "").lower() in allowed_hosts
    )


def _link_texts_for(host, overrides):
    key = _site_key(host)
    table = dict(DEFAULT_LINK_TEXTS)
    table.update({_site_key(k): list(v) for k, v in (overrides or {}).items()})
    if key in table:
        return table[key]
    for site, texts in table.items():
        if key.endswith("." + site):
            return texts
    return []


def _scroll_plan(rng, unit):
    """Varied scroll amounts: mostly down, occasionally a small scroll up."""
    amounts = []
    for i in range(rng.randint(3, 7)):
        if i > 0 and rng.random() < 0.2:
            amount = rng.randint(1, 2) * unit
        else:
            amount = -rng.randint(2, 6) * unit
        amounts.append(max(-MAX_SCROLL_ABS, min(MAX_SCROLL_ABS, amount)))
    return amounts


def _browser_settings(config):
    browser = config.get("browser", {})
    settings = {key: browser.get(key, default) for key, default in DEFAULTS.items()}
    settings["websites"] = list(browser.get("websites", []))
    settings["minimum_links"] = int(browser.get("minimum_links_per_session", 1))
    settings["link_texts"] = browser.get("link_texts", {})
    return settings


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

class _Session:
    """Mutable state for one workflow run, plus the actions on the browser."""

    def __init__(self, context, gui, windows, clock, rng):
        self.behaviour = context.behaviour
        self.output_dir = Path(context.output_dir)
        self.gui = gui
        self.windows = windows
        self.clock = clock
        self.rng = rng
        self.cfg = _browser_settings(context.config)
        self.deadline = clock.now() + float(self.cfg["time_budget_seconds"])
        self.started_at = clock.timestamp()

        self.actions = []
        self.errors = []
        self.visits = []
        self.link_results = []
        self.handle = None
        self.browser = None
        self.closed = False
        self.links_followed = 0

    # -- utilities ---------------------------------------------------------

    def time_left(self):
        return self.deadline - self.clock.now()

    def wait_for(self, predicate, timeout):
        """Poll predicate until truthy or timeout; returns the last value."""
        end = self.clock.now() + timeout
        while True:
            value = predicate()
            if value or self.clock.now() >= end:
                return value
            self.clock.sleep(POLL_SECONDS)

    def ensure_focus(self):
        """Bring our window to the front; raise if it has gone."""
        if not self.windows.exists(self.handle):
            raise BrowserWindowLost()
        for _ in range(3):
            if self.windows.active() == self.handle or self.windows.activate(self.handle):
                return True
            self.clock.sleep(POLL_SECONDS)
        return False

    def current_title(self):
        title = self.windows.title(self.handle)
        if title is None:
            raise BrowserWindowLost()
        return title

    def wait_for_title_change(self, old_title, timeout):
        """Wait until our window title differs from old_title and settles.

        Returns the new title, or None on timeout. Raises if the window closes.
        """
        end = self.clock.now() + min(timeout, max(self.time_left(), 1.0))
        last, stable_since = None, None
        while self.clock.now() < end:
            title = self.current_title()
            if title.strip() and title != old_title:
                if title == last:
                    if self.clock.now() - stable_since >= SETTLE_SECONDS:
                        return title
                else:
                    last, stable_since = title, self.clock.now()
            self.clock.sleep(POLL_SECONDS)
        return last

    # -- launch ------------------------------------------------------------

    def launch(self):
        for command in BROWSER_COMMANDS:
            if self.launch_via_run_dialog(command):
                return True
        self.errors.append("browser_launch_failed:no_browser_window")
        return False

    def launch_via_run_dialog(self, command):
        before = set(self.windows.list_windows())
        self.gui.hotkey("win", "r")
        run_title = str(self.cfg["run_dialog_title"]).lower()
        run_dialog = self.wait_for(
            lambda: self.find_new_window(before, lambda t: _normalize_title(t) == run_title),
            float(self.cfg["run_dialog_timeout_seconds"]),
        )
        if not run_dialog:
            self.errors.append(f"run_dialog_not_found:{command}")
            return False

        self.behaviour.type_text(command)
        self.gui.press("enter")
        before.add(run_dialog)

        handle = self.wait_for(
            lambda: self.find_new_window(before, lambda t: _browser_kind(t) == command),
            float(self.cfg["launch_timeout_seconds"]),
        )
        if not handle:
            self.dismiss_launch_error(before, command)
            self.errors.append(f"browser_launch_timeout:{command}")
            return False

        self.handle, self.browser = handle, command
        self.actions.append(f"browser_launched:{command}")
        # Let the initial tab title settle before we start typing.
        self.wait_for_title_change(None, SETTLE_SECONDS * 3)
        return True

    def find_new_window(self, before, title_matches):
        for handle, title in self.windows.list_windows().items():
            if handle not in before and title_matches(title):
                return handle
        return None

    def dismiss_launch_error(self, before, command):
        # Windows shows a message box titled with the command when not found.
        box = self.find_new_window(before, lambda t: _normalize_title(t) == command)
        if box and self.windows.activate(box):
            self.gui.press("esc")
            self.errors.append(f"browser_not_found:{command}")

    # -- navigation --------------------------------------------------------

    def visit(self, url):
        """Navigate to url; returns True if the page loaded and is usable."""
        host = urlsplit(url).hostname
        record = {"url": url, "started_at": self.clock.timestamp(), "status": "pending"}
        self.visits.append(record)

        if not self.ensure_focus():
            return self.fail_visit(record, "focus_lost", url)

        self.behaviour.pause("navigate")
        old_title = self.current_title()
        self.gui.hotkey("ctrl", "l")
        self.behaviour.type_text(url)
        self.gui.press("delete")  # drop any inline autocomplete suffix
        self.gui.press("enter")

        title = self.wait_for_title_change(old_title, float(self.cfg["page_load_timeout_seconds"]))
        if title is None:
            return self.fail_visit(record, "page_load_timeout", url)
        if _is_cert_error(title):
            return self.fail_visit(record, "certificate_error", url)
        if self.cfg["treat_host_title_as_error"] and _title_is_bare_host(title, host):
            return self.fail_visit(record, "page_load_failed", url)

        record["status"] = "visited"
        self.actions.append(f"visited:{url}")
        return True

    def fail_visit(self, record, reason, url):
        record["status"] = reason
        self.errors.append(f"{reason}:{url}")
        return False

    def read_page(self, label, record=None):
        """Read, scroll a varied amount, then review the current page."""
        self.behaviour.pause("reading")
        center = self.windows.center(self.handle)
        if center:
            x, y = center
            self.gui.move_to(x + self.rng.randint(-80, 80), y + self.rng.randint(-60, 60))

        amounts = _scroll_plan(self.rng, int(self.cfg["scroll_unit"]))
        for i, amount in enumerate(amounts):
            if i > 0:
                self.behaviour.pause("scroll")
            self.gui.scroll(amount)
        self.behaviour.pause("review")

        self.actions.append(f"scrolled:{label}:x{len(amounts)}")
        if record is not None:
            record["scroll_amounts"] = amounts

    # -- links -------------------------------------------------------------

    def follow_link(self, url):
        """Try each candidate link text once on the current page."""
        host = urlsplit(url).hostname
        site = _site_key(host)
        candidates = _link_texts_for(host, self.cfg["link_texts"])
        if not candidates:
            self.record_link(site, None, "skipped", "no_candidate_link_texts")
            return False

        for text in candidates:
            if self.time_left() <= 0:
                self.record_link(site, text, "skipped", "time_budget_exceeded")
                return False
            if self.try_link_text(site, text):
                return True

        self.errors.append(f"link_not_followed:{site}:no_candidate_worked")
        return False

    def try_link_text(self, site, text):
        if not self.ensure_focus():
            self.record_link(site, text, "failed", "focus_lost")
            return False

        old_title = self.current_title()
        self.gui.press("home")  # start the find from the top (nav links)
        self.gui.hotkey("ctrl", "f")
        self.behaviour.type_text(text)
        self.gui.press("esc")  # closing find focuses a matched link
        self.behaviour.pause("navigate")
        self.gui.press("enter")

        title = self.wait_for_title_change(old_title, float(self.cfg["link_load_timeout_seconds"]))
        if title is None:
            self.record_link(site, text, "failed", "no_title_change")
            return False
        if _is_cert_error(title):
            self.record_link(site, text, "failed", "certificate_error")
            self.errors.append(f"certificate_error:link:{site}:{text}")
            self.go_back(site, title)
            return False

        self.links_followed += 1
        self.record_link(site, text, "followed", None)
        self.actions.append(f"link_followed:{site}:{text}")
        self.read_page(f"{site}:{text}")
        self.go_back(site, title)
        return True

    def record_link(self, site, text, result, reason):
        self.link_results.append({
            "site": site,
            "link_text": text,
            "result": result,
            "reason": reason,
            "at": self.clock.timestamp(),
        })

    def go_back(self, site, current):
        if not self.ensure_focus():
            return
        self.gui.hotkey("alt", "left")
        if self.wait_for_title_change(current, float(self.cfg["link_load_timeout_seconds"])):
            self.actions.append(f"navigated_back:{site}")
        else:
            self.errors.append(f"back_navigation_timeout:{site}")

    # -- close -------------------------------------------------------------

    def close(self):
        """Close only our window: Ctrl+Shift+W, then Alt+F4 as fallback."""
        if self.handle is None:
            return
        timeout = float(self.cfg["close_timeout_seconds"])
        for keys in (("ctrl", "shift", "w"), ("alt", "f4")):
            if not self.windows.exists(self.handle):
                break
            # Never send close keys unless our own window is in front.
            if not self.windows.activate(self.handle):
                continue
            self.gui.hotkey(*keys)
            self.wait_for(lambda: not self.windows.exists(self.handle), timeout)

        if self.windows.exists(self.handle):
            self.errors.append("browser_close_failed")
        else:
            self.closed = True
            self.actions.append("browser_closed")

    # -- results -----------------------------------------------------------

    def visited_count(self):
        return sum(1 for v in self.visits if v["status"] == "visited")

    def is_success(self):
        return (
            self.visited_count() >= MIN_SITES
            and self.links_followed >= self.cfg["minimum_links"]
            and self.closed
        )

    def write_report(self, success):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = (self.output_dir / "browser_session.json").resolve()
        report = {
            "module": "web_browser",
            "started_at": self.started_at,
            "finished_at": self.clock.timestamp(),
            "browser": self.browser,
            "success": success,
            "sites_visited": self.visited_count(),
            "links_followed": self.links_followed,
            "minimum_links_per_session": self.cfg["minimum_links"],
            "browser_closed": self.closed,
            "visits": self.visits,
            "link_results": self.link_results,
            "actions_completed": self.actions,
            "errors": self.errors,
        }
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return str(path)


def _run_workflow(context, gui, windows, clock, rng=None):
    """Run the bounded browser workflow with injected adapters."""
    session = _Session(context, gui, windows, clock, rng or random.Random())
    failsafe = getattr(gui, "failsafe_exception", None) or _NeverRaised
    aborted = False

    try:
        _drive(session)
    except failsafe as exc:
        # The operator slammed the mouse into a corner: stop touching the GUI.
        aborted = True
        session.errors.append(f"failsafe_triggered:{exc}")
    except BrowserWindowLost:
        session.errors.append("browser_window_lost")
    except Exception as exc:
        session.errors.append(f"unexpected_error:{type(exc).__name__}:{exc}")

    if not aborted and not session.closed:
        try:
            session.close()
        except failsafe as exc:
            session.errors.append(f"failsafe_triggered:{exc}")
        except Exception as exc:
            session.errors.append(f"close_error:{type(exc).__name__}:{exc}")

    success = session.is_success()
    artifacts = []
    try:
        artifacts.append(session.write_report(success))
    except OSError as exc:
        session.errors.append(f"report_write_failed:{exc}")

    return TaskResult(
        success=success,
        actions_completed=list(session.actions),
        artifacts=artifacts,
        errors=list(session.errors),
    )


class _NeverRaised(Exception):
    """Placeholder when an adapter has no fail-safe exception type."""


def _drive(session):
    """Launch, visit sites in varied order, follow links, then close."""
    allowed, config_errors = _allowed_hosts(session.cfg["websites"])
    session.errors.extend(config_errors)

    sites = [u for u in session.cfg["websites"] if _validate_url(u, allowed)]
    if len(sites) < MIN_SITES:
        session.errors.append(f"not_enough_valid_sites:{len(sites)}")
        return
    session.rng.shuffle(sites)
    target_sites = max(MIN_SITES, int(session.cfg["target_sites_per_session"]))

    if not session.launch():
        return
    session.behaviour.pause("default")

    for url in sites:
        if session.visited_count() >= target_sites and session.links_followed >= session.cfg["minimum_links"]:
            break
        if session.time_left() <= 0:
            session.errors.append("time_budget_exceeded")
            break
        # Defence in depth: never type a URL that is not https + listed.
        if not _validate_url(url, allowed):
            session.errors.append(f"url_rejected:{url}")
            continue
        if not session.visit(url):
            continue
        session.read_page(url, session.visits[-1])
        if session.links_followed < session.cfg["minimum_links"]:
            session.follow_link(url)

    session.behaviour.pause("default")
    session.close()


# ---------------------------------------------------------------------------
# Local dev runner: python -m modules.web_browser
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import time

    class _DevBehaviour:
        """Stand-in until Member 3's BehaviourEngine is merged."""

        _RANGES = {
            "navigate": (0.6, 1.5),
            "reading": (2.0, 4.0),
            "scroll": (0.6, 1.6),
            "review": (1.0, 2.5),
            "default": (0.5, 1.2),
        }

        def pause(self, action="default"):
            low, high = self._RANGES.get(action, self._RANGES["default"])
            time.sleep(random.uniform(low, high))

        def type_text(self, text):
            import pyautogui

            pyautogui.write(text, interval=random.uniform(0.04, 0.09))

    root = Path(__file__).resolve().parent.parent
    config = json.loads((root / "config" / "settings.json").read_text(encoding="utf-8-sig"))
    ctx = SessionContext(
        config=config,
        output_dir=root / "outputs" / "dev_browser",
        behaviour=_DevBehaviour(),
    )
    print(run(ctx))
