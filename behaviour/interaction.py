"""Shared BehaviourProtocol implementation; standard library only.

    engine = BehaviourEngine.from_config(config.get('behaviour', {}), seed=42)
    engine.pause(action='reading')
    engine.type_text('Weekly report\nStatus: on schedule.')

Inject keyboard.write/press, sleeper(seconds), clock() and wall_clock() for tests.
The engine is sequential, not thread-safe. Events omit typed text and key values.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import time

from .timing import TimingModel, summarise_intervals
from .typing import CancelledError, TypingSettings, WindowsKeyboard, type_characters, validate_text


class BehaviourEngine:
    def __init__(self, seed: int = 42, *, sleeper=time.sleep, clock=time.monotonic,
                 wall_clock=None, rng=None, keyboard=None, profiles=None,
                 enable_variable_pauses: bool = True, enable_typing_corrections: bool = True,
                 correction_probability: float = 0.018, cancel_check=None,
                 event_sink=None, event_limit: int = 10000):
        if not isinstance(event_limit, int) or event_limit < 1:
            raise ValueError("event_limit must be a positive integer")
        self.rng = rng if rng is not None else random.Random(seed)
        self.timing = TimingModel(self.rng, profiles=profiles, variable=enable_variable_pauses)
        # Validate the setting even when corrections are disabled.
        TypingSettings(correction_probability)
        self.typing = TypingSettings(correction_probability if enable_typing_corrections else 0)
        self.sleeper, self.clock = sleeper, clock
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._keyboard = keyboard
        self.cancel_check, self.event_sink = cancel_check, event_sink
        self.events = deque(maxlen=event_limit)
        self.dropped_events = 0
        self._guards = []
        self._cancelled = False
        self._sequence = 0

    @classmethod
    def from_config(cls, config: dict | None = None, **kwargs):
        values = dict(config or {})
        profile = values.pop("profile", "office_worker")
        if profile != "office_worker":
            raise ValueError("Only the office_worker behaviour profile is supported")
        allowed = {"enable_variable_pauses", "enable_typing_corrections", "correction_probability", "seed"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"Unknown behaviour settings: {sorted(unknown)}")
        for name in ("enable_variable_pauses", "enable_typing_corrections"):
            if name in values and not isinstance(values[name], bool):
                raise ValueError(f"{name} must be a boolean")
        values.update(kwargs)
        return cls(**values)

    @property
    def keyboard(self):
        if self._keyboard is None:
            self._keyboard = WindowsKeyboard()
        return self._keyboard

    def cancel(self):
        self._cancelled = True

    def check_cancel(self):
        if self._cancelled or (self.cancel_check and self.cancel_check()):
            raise CancelledError("Behaviour run cancelled")
        if self._keyboard is not None and hasattr(self._keyboard, "check_cancel"):
            self._keyboard.check_cancel()

    def _check(self):
        self.check_cancel()
        for guard in self._guards:
            guard()

    @contextmanager
    def input_guard(self, guard):
        """Optional extension; office uses it to verify focus before every key."""
        self._guards.append(guard)
        try:
            yield
        finally:
            self._guards.remove(guard)

    def _record(self, action, start, *, ok=True, **details):
        self._sequence += 1
        event = {"ts_utc": self.wall_clock().isoformat(), "sequence": self._sequence,
                 "module": "behaviour", "action": action,
                 "duration_ms": round(max(0, self.clock() - start) * 1000, 3),
                 "ok": ok, "details": details}
        if len(self.events) == self.events.maxlen:
            self.dropped_events += 1
        self.events.append(event)
        if self.event_sink:
            self.event_sink(dict(event))

    def _delay(self, action):
        self._check()
        duration = self.timing.sample(action)
        start = self.clock()
        remaining = duration
        try:
            # Short chunks keep Escape/caller cancellation responsive during reading.
            while remaining > 1e-9:
                self._check()
                chunk = min(0.1, remaining)
                self.sleeper(chunk)
                remaining -= chunk
            self._check()
        except Exception:
            self._record("pause", start, ok=False, context=self.timing.context(action),
                         planned_s=duration)
            raise
        self._record("pause", start, context=self.timing.context(action), planned_s=duration)

    def pause(self, action: str = "default") -> None:
        self._delay(action)

    def type_text(self, text: str) -> None:
        text = validate_text(text, self.typing.max_characters)
        start = self.clock()
        try:
            self._check()
            corrections = 0
            if text:
                corrections = type_characters(text, keyboard=self.keyboard, rng=self.rng,
                    delay=self._delay, check=self._check,
                    correction_probability=self.typing.correction_probability)
        except Exception as exc:
            self._record("type_text", start, ok=False, characters=len(text),
                         error_type=type(exc).__name__)
            raise
        self._record("type_text", start, characters=len(text), corrections=corrections)

    def timing_summary(self):
        groups = {}
        for event in self.events:
            if event["action"] == "pause" and event["ok"]:
                groups.setdefault(event["details"]["context"], []).append(event["duration_ms"] / 1000)
        return {"source": "application interaction timings, not Sysmon or physical keystrokes",
                "retained_events": len(self.events), "dropped_events": self.dropped_events,
                "contexts": {name: summarise_intervals(values) for name, values in groups.items()}}

    def export_events(self, path: str | Path):
        """Optional caller-selected output; refuses to overwrite any existing file."""
        with Path(path).open("x", encoding="utf-8") as stream:
            for event in self.events:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
