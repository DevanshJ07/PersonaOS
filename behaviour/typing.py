"""Typing model and explicit Windows input adapter. No hooks or clipboard access.

Windows SendInput is ordinary, detectable synthetic input. It does not provide
physical-HID emulation or change process ancestry. Imports are headless-safe.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import math
import sys
from typing import Protocol


class CancelledError(RuntimeError):
    """Stop requested by caller, Escape key or upper-left screen corner."""


class Keyboard(Protocol):
    def write(self, text: str) -> None: ...
    def press(self, key: str) -> None: ...


@dataclass(frozen=True)
class TypingSettings:
    correction_probability: float = 0.018
    max_characters: int = 20000

    def __post_init__(self):
        if not math.isfinite(self.correction_probability) or not 0 <= self.correction_probability <= 1:
            raise ValueError("correction_probability must be between zero and one")
        if not isinstance(self.max_characters, int) or self.max_characters < 1:
            raise ValueError("max_characters must be a positive integer")


def validate_text(text: str, limit: int = 20000) -> str:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if len(text) > limit:
        raise ValueError(f"text exceeds {limit} characters")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 or
           0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise ValueError("Unsupported control character or unpaired Unicode surrogate")
    return text


def type_characters(text, *, keyboard, rng, delay, check, correction_probability):
    """Correct before word boundaries, where editor autocorrect usually triggers."""
    corrections = 0
    for char in text:
        check()
        delay("typing")
        check()
        if char.isascii() and char.isalpha() and rng.random() < correction_probability:
            choices = "abcdefghijklmnopqrstuvwxyz".replace(char.lower(), "")
            typo = rng.choice(choices)
            keyboard.write(typo.upper() if char.isupper() else typo)
            delay("correcting")
            check()
            keyboard.press("backspace")
            delay("correcting")
            corrections += 1
        check()
        if char == "\n":
            keyboard.press("enter")
        elif char == "\t":
            keyboard.press("tab")
        else:
            keyboard.write(char)
        if char == "\n":
            delay("paragraph")
        elif char in ".,;:!?":
            delay("punctuation")
    return corrections


class WindowsKeyboard:
    """Native input with optional per-input focus guard. Windows only at use time.

    Hold Escape or move the VM cursor to its upper-left corner to stop.
    Use at the same privilege level as the target app; SendInput obeys UIPI.
    """

    KEYS = {"backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10,
            "ctrl": 0x11, "alt": 0x12, "escape": 0x1B, "home": 0x24,
            "end": 0x23, "left": 0x25, "right": 0x27, "delete": 0x2E,
            "f12": 0x7B, "win": 0x5B}

    def __init__(self, *, focus_guard=None):
        if sys.platform != "win32":
            raise RuntimeError("Live keyboard input requires the authorized Windows VM")
        self.focus_guard = focus_guard
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        ulong_ptr = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ulong_ptr)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD)]

        class PAYLOAD(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("payload",)
            _fields_ = [("type", wintypes.DWORD), ("payload", PAYLOAD)]

        self.INPUT, self.KEYBDINPUT, self.MOUSEINPUT = INPUT, KEYBDINPUT, MOUSEINPUT
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL

    def check_cancel(self):
        if self.user32.GetAsyncKeyState(0x1B) & 0x8000:
            raise CancelledError("Escape held; desktop automation stopped")
        position = wintypes.POINT()
        if self.user32.GetCursorPos(ctypes.byref(position)) and position.x <= 1 and position.y <= 1:
            raise CancelledError("Cursor in upper-left corner; desktop automation stopped")

    def _check(self):
        self.check_cancel()
        if self.focus_guard:
            self.focus_guard()
        if any(self.user32.GetAsyncKeyState(k) & 0x8000 for k in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
            raise RuntimeError("Release Shift/Ctrl/Alt/Windows keys before automation")

    def _send(self, packets):
        self._check()
        array = (self.INPUT * len(packets))(*packets)
        sent = self.user32.SendInput(len(array), array, ctypes.sizeof(self.INPUT))
        if sent != len(array):
            # Release virtual keys even after a partial batch; never leave Ctrl held.
            releases = [self._key(p.ki.wVk, flags=2) for p in packets
                        if p.type == 1 and p.ki.wVk and not p.ki.dwFlags & 2]
            if releases:
                cleanup = (self.INPUT * len(releases))(*releases)
                self.user32.SendInput(len(cleanup), cleanup, ctypes.sizeof(self.INPUT))
            raise RuntimeError("Windows rejected input; check focus, desktop lock and privilege level")

    def _key(self, vk=0, scan=0, flags=0):
        item = self.INPUT(type=1)
        item.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return item

    def _vk(self, key):
        if key.lower() in self.KEYS:
            return self.KEYS[key.lower()]
        if len(key) == 1 and key.isascii() and key.isalnum():
            return ord(key.upper())
        raise ValueError(f"Unsupported key: {key}")

    def write(self, text):
        for char in validate_text(text):
            if char in "\n\t":
                self.press("enter" if char == "\n" else "tab")
                continue
            raw = char.encode("utf-16-le")
            packets = []
            for index in range(0, len(raw), 2):
                unit = int.from_bytes(raw[index:index + 2], "little")
                packets.extend([self._key(scan=unit, flags=4), self._key(scan=unit, flags=6)])
            self._send(packets)

    def press(self, key):
        vk = self._vk(key)
        extended = 1 if key.lower() in {"home", "end", "left", "right", "delete"} else 0
        self._send([self._key(vk, flags=extended), self._key(vk, flags=extended | 2)])

    def hotkey(self, *keys):
        codes = [(self._vk(k), 1 if k.lower() in {"home", "end", "left", "right", "delete"} else 0)
                 for k in keys]
        self._send([self._key(vk, flags=flags) for vk, flags in codes] +
                   [self._key(vk, flags=flags | 2) for vk, flags in reversed(codes)])

    def click(self, x, y):
        self._check()
        if not self.user32.SetCursorPos(int(x), int(y)):
            raise RuntimeError("Unable to position pointer on the desktop control")
        packets = []
        for flag in (2, 4):
            item = self.INPUT(type=0)
            item.mi = self.MOUSEINPUT(0, 0, 0, flag, 0, 0)
            packets.append(item)
        self._send(packets)
