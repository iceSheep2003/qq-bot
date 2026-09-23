"""The emotion package reads its own environment.

Keeping these knobs here rather than in qunbot/config.py is deliberate: the
core Config stays free of mood fields, and this file is deleted along with
the rest of the package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _number(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class EmotionConfig:
    enabled: bool
    auto_enabled: bool
    db_path: Path
    decay_minutes: int
    sensitivity: float
    min_sociability: int
    # Scale on the coupling matrix. 1.0 is the shipped feel; 0.0 removes the
    # cross-dimension push entirely. Together with decay_minutes this decides
    # whether a mood relaxes back or latches at an extreme — see
    # ``EmotionPolicy``/``loop_gain`` and the calibration tests.
    coupling: float = 1.0

    @classmethod
    def from_env(cls) -> EmotionConfig:
        return cls(
            enabled=_flag("BOT_MOOD_ENABLED", True),
            auto_enabled=_flag("BOT_MOOD_AUTO_ENABLED", True),
            db_path=Path(
                os.getenv("BOT_MOOD_DB_PATH", "./data/emotion.sqlite3")
            ),
            decay_minutes=int(
                _number("BOT_MOOD_DECAY_MINUTES", 180, low=5, high=10080)
            ),
            sensitivity=_number("BOT_MOOD_SENSITIVITY", 1.0, low=0.0, high=3.0),
            min_sociability=int(
                _number("BOT_MOOD_PROACTIVE_MIN_SOCIABILITY", 35, low=0, high=100)
            ),
            coupling=_number("BOT_MOOD_COUPLING", 1.0, low=0.0, high=2.0),
        )
