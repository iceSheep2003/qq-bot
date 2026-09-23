"""The slang package reads its own environment.

Learning from untrusted group messages is the reason this file exists rather
than fields on the core ``Config``: the switch is off unless a deployer
explicitly turns it on, and nothing about it belongs in the core.
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


def _count(name: str, default: int, *, low: int, high: int) -> int:
    return int(_number(name, default, low=low, high=high))


@dataclass(frozen=True)
class SlangConfig:
    # Off by default. Learning from group messages is opt-in, always.
    enabled: bool = False
    scan_interval_seconds: int = 900
    window_messages: int = 200
    # How many times a term must be seen before it is even worth storing. A
    # single occurrence is indistinguishable from a typo; set to 1 to record
    # singletons as candidates (still never auto-active — see below).
    min_occurrences: int = 2
    min_term_chars: int = 2
    max_term_chars: int = 12
    max_ngram: int = 4
    max_message_chars: int = 200
    max_sample_chars: int = 60
    max_samples: int = 5
    max_candidates: int = 400
    max_terms: int = 24
    # Review gates. 0.5 is about "three times, by two people": enough to read
    # as a group saying rather than an accident.
    understand_confidence: float = 0.5
    # Imitation is the conservative tier: high confidence *and* reviewed.
    imitate_confidence: float = 0.75
    # Both default to "no". An unreviewed term cannot reach the prompt, and
    # nothing learned is styled after unless the deployer says so.
    allow_unreviewed: bool = False
    allow_imitation: bool = False
    review_path: Path = Path("./config/slang_review.json")
    wordlist_path: Path | None = None

    @classmethod
    def from_env(cls) -> SlangConfig:
        wordlist = os.getenv("BOT_SLANG_WORDLIST_PATH", "").strip()
        return cls(
            enabled=_flag("BOT_SLANG_ENABLED", False),
            scan_interval_seconds=_count(
                "BOT_SLANG_SCAN_INTERVAL_SECONDS", 900, low=30, high=86400
            ),
            window_messages=_count(
                "BOT_SLANG_WINDOW_MESSAGES", 200, low=10, high=2000
            ),
            min_occurrences=_count("BOT_SLANG_MIN_OCCURRENCES", 2, low=1, high=100),
            max_term_chars=_count("BOT_SLANG_MAX_TERM_CHARS", 12, low=2, high=24),
            max_ngram=_count("BOT_SLANG_MAX_NGRAM", 4, low=2, high=6),
            max_message_chars=_count(
                "BOT_SLANG_MAX_MESSAGE_CHARS", 200, low=20, high=2000
            ),
            max_samples=_count("BOT_SLANG_MAX_SAMPLES", 5, low=0, high=20),
            max_candidates=_count("BOT_SLANG_MAX_CANDIDATES", 400, low=10, high=5000),
            max_terms=_count("BOT_SLANG_MAX_TERMS", 24, low=1, high=100),
            understand_confidence=_number(
                "BOT_SLANG_UNDERSTAND_CONFIDENCE", 0.5, low=0.0, high=1.0
            ),
            imitate_confidence=_number(
                "BOT_SLANG_IMITATE_CONFIDENCE", 0.75, low=0.0, high=1.0
            ),
            allow_unreviewed=_flag("BOT_SLANG_ALLOW_UNREVIEWED", False),
            allow_imitation=_flag("BOT_SLANG_ALLOW_IMITATION", False),
            review_path=Path(
                os.getenv("BOT_SLANG_REVIEW_PATH", "./config/slang_review.json")
            ),
            wordlist_path=Path(wordlist) if wordlist else None,
        )
