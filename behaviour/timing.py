"""Bounded, seeded timing. Defaults are design parameters, not human calibration."""
from __future__ import annotations

from dataclasses import dataclass
import math
import random
import statistics
from typing import Mapping


@dataclass(frozen=True)
class PauseProfile:
    minimum: float
    median: float
    maximum: float
    sigma: float = 0.65

    def __post_init__(self):
        if not all(math.isfinite(v) for v in
                   (self.minimum, self.median, self.maximum, self.sigma)):
            raise ValueError("Timing parameters must be finite")
        if not 0 < self.minimum <= self.median <= self.maximum or self.sigma < 0:
            raise ValueError("Require 0 < minimum <= median <= maximum and sigma >= 0")


DEFAULT_PROFILES = {
    "default": PauseProfile(0.25, 0.65, 2.5),
    "typing": PauseProfile(0.04, 0.13, 0.65, 0.5),
    "reading": PauseProfile(1.5, 4.0, 12.0),
    "navigate": PauseProfile(0.4, 1.1, 4.0),
    "scroll": PauseProfile(0.3, 0.8, 3.0),
    "review": PauseProfile(1.0, 3.0, 9.0),
    "correcting": PauseProfile(0.15, 0.38, 1.2),
    "punctuation": PauseProfile(0.12, 0.35, 1.2),
    "paragraph": PauseProfile(0.5, 1.4, 4.0),
    "wait": PauseProfile(0.05, 0.1, 0.2, 0.25),
}
ALIASES = {
    "read": "reading", "navigation": "navigate", "navigating": "navigate",
    "scrolling": "scroll", "reviewing": "review", "compose": "typing",
    "composing": "typing", "correction": "correcting", "open": "navigate",
    "save": "navigate", "close": "navigate", "page_load": "navigate",
}


class TimingModel:
    """Clipped log-normal samples in seconds; same seed + calls => same samples."""

    def __init__(self, rng: random.Random | None = None, *,
                 profiles: Mapping[str, PauseProfile] | None = None,
                 variable: bool = True):
        self.rng = rng if rng is not None else random.Random(42)
        self.profiles = dict(DEFAULT_PROFILES)
        if profiles:
            self.profiles.update(profiles)
        if not all(isinstance(p, PauseProfile) for p in self.profiles.values()):
            raise TypeError("profiles must contain PauseProfile values")
        self.variable = variable

    def context(self, action: str) -> str:
        value = ALIASES.get(action.strip().lower(), action.strip().lower())
        return value if value in self.profiles else "default"

    def sample(self, action: str = "default") -> float:
        profile = self.profiles[self.context(action)]
        raw = (self.rng.lognormvariate(math.log(profile.median), profile.sigma)
               if self.variable else profile.median)
        return max(profile.minimum, min(profile.maximum, raw))


def summarise_intervals(values) -> dict:
    """Descriptive timing only. CV is not proof of human indistinguishability."""
    data = [float(value) for value in values]
    if any(not math.isfinite(v) or v < 0 for v in data):
        raise ValueError("Intervals must be finite and nonnegative")
    if not data:
        return {"count": 0, "mean_s": None, "stdev_s": None, "cv": None,
                "min_s": None, "max_s": None}
    mean = statistics.mean(data)
    std = statistics.pstdev(data)
    return {"count": len(data), "mean_s": mean, "stdev_s": std,
            "cv": std / mean if mean else None,
            "min_s": min(data), "max_s": max(data)}
