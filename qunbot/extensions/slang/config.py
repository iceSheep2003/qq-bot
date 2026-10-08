"""The slang package reads its own environment.

Learning from untrusted group messages is the reason this file exists rather
than fields on the core ``Config``: the switch is off unless a deployer
explicitly turns it on, and nothing about it belongs in the core.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ...storage.slang import INFERENCE_THRESHOLDS


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _csv_ints(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """A comma-separated list of thresholds. Empty or non-numeric is refused.

    Silently falling back to the default would make a typo look like it had
    taken effect, and the whole point of the knob is to tune spend.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        values = tuple(int(part) for part in raw.split(",") if part.strip())
    except ValueError:
        raise ValueError(f"{name} must be a comma-separated list of integers") from None
    if not values or any(value < 1 for value in values):
        raise ValueError(f"{name} must be positive integers")
    return values


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
    # The evidence floor for *storage*. A term at or above this count is
    # written to the table, singletons included. Counting has to survive
    # between scan windows for a sparse-but-steady term to ever accumulate:
    # dropping it at the door reset its tally to 1 on every pass, so a group's
    # word used once per window stayed invisible forever.
    store_occurrences: int = 1
    # The evidence floor for *promotion*. Stored-but-unpromoted terms are kept
    # and counted but never glossed, never injected and never offered for
    # review: a single occurrence is a typo far more often than a word.
    min_occurrences: int = 2
    # Confidence half-life for a candidate nobody has used lately. A term is
    # evidence about now, so an old but briefly-famous one should fade rather
    # than hold a prompt slot forever. Reviewed rows never decay.
    decay_half_life_seconds: int = 2592000  # 30 days

    # Meaning inference — the only part of this package that spends money.
    # Every knob here is a cost gate, because the default deployment builds its
    # model client without a budget policy: the only ceiling on what learning
    # costs is the one this file sets.
    gloss_enabled: bool = True
    # Minimum spacing between inference passes per scope, persisted. A model
    # that is misconfigured or down costs one attempt per interval, not one
    # per scan.
    gloss_interval_seconds: int = 3600
    # Terms per model call. Three calls per batch, never three per term.
    gloss_batch_size: int = 12
    # Hard ceiling on terms examined per scope per pass.
    gloss_max_per_scan: int = 24
    # Occurrence counts that buy a (re-)inference. A term is re-examined as it
    # earns evidence instead of being judged on its first sighting.
    infer_thresholds: tuple[int, ...] = INFERENCE_THRESHOLDS
    # Ask the model to propose terms when counting found nothing. Off is a
    # reasonable deployment: it is the one path that mines without evidence.
    llm_fallback: bool = True
    # A meaning is shown beside its term in a prompt, so it is kept short.
    max_meaning_chars: int = 24
    # Off by default: a deployer's correction is not the model's to revise.
    overwrite_human_meanings: bool = False
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
            store_occurrences=_count(
                "BOT_SLANG_STORE_OCCURRENCES", 1, low=1, high=100
            ),
            min_occurrences=_count("BOT_SLANG_MIN_OCCURRENCES", 2, low=1, high=100),
            decay_half_life_seconds=_count(
                "BOT_SLANG_DECAY_HALF_LIFE_SECONDS", 2592000, low=3600, high=31536000
            ),
            gloss_enabled=_flag("BOT_SLANG_GLOSS_ENABLED", True),
            gloss_interval_seconds=_count(
                "BOT_SLANG_GLOSS_INTERVAL_SECONDS", 3600, low=60, high=86400
            ),
            gloss_batch_size=_count("BOT_SLANG_GLOSS_BATCH", 12, low=1, high=50),
            gloss_max_per_scan=_count(
                "BOT_SLANG_GLOSS_MAX_PER_SCAN", 24, low=0, high=200
            ),
            infer_thresholds=_csv_ints(
                "BOT_SLANG_INFER_THRESHOLDS", INFERENCE_THRESHOLDS
            ),
            llm_fallback=_flag("BOT_SLANG_LLM_FALLBACK", True),
            max_meaning_chars=_count(
                "BOT_SLANG_MAX_MEANING_CHARS", 24, low=4, high=80
            ),
            overwrite_human_meanings=_flag(
                "BOT_SLANG_OVERWRITE_HUMAN_MEANINGS", False
            ),
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
