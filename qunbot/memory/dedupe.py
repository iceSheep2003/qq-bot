"""De-duplication and conflict decisions for the write path.

Exact repetition is handled by the store's unique dedupe key. This module adds
fuzzy near-duplicate detection so paraphrases do not pile up, and decides what
a re-observation means. Pure functions over rows, no storage import.
"""

from __future__ import annotations

from dataclasses import dataclass

from .text import similarity

#: At or above this the new fact is treated as the same fact restated.
DUPLICATE_THRESHOLD = 0.85
#: Between these two values the fact is a rephrasing of an existing one.
PARAPHRASE_THRESHOLD = 0.55

DECISION_NEW = "new"
DECISION_DUPLICATE = "duplicate"
DECISION_PARAPHRASE = "paraphrase"
DECISION_CONFLICT = "conflict"


@dataclass(frozen=True)
class DedupeDecision:
    kind: str
    memory_id: int | None = None
    score: float = 0.0

    @property
    def is_new(self) -> bool:
        return self.kind == DECISION_NEW


def decide(existing: list[dict], content: str) -> DedupeDecision:
    """Compare ``content`` against the live rows already held for one subject.

    A paraphrase keeps the stored wording and just reinforces it, so the same
    preference stated twice does not create two rows. Contradiction detection
    is the caller's job (``supersede``); this only reports the closest match.
    """
    best: DedupeDecision | None = None
    for row in existing:
        score = similarity(str(row.get("content", "")), content)
        if best is None or score > best.score:
            best = DedupeDecision(DECISION_NEW, row.get("id"), score)
    if best is None:
        return DedupeDecision(DECISION_NEW, None, 0.0)
    if best.score >= DUPLICATE_THRESHOLD:
        return DedupeDecision(DECISION_DUPLICATE, best.memory_id, best.score)
    if best.score >= PARAPHRASE_THRESHOLD:
        return DedupeDecision(DECISION_PARAPHRASE, best.memory_id, best.score)
    return DedupeDecision(DECISION_NEW, None, best.score)


def contradiction(left: str, right: str) -> bool:
    """Cheap lexical cue: same topic but one side negated."""
    negations = ("不", "没", "别", "讨厌", "拒绝", "not ", "no ", "doesn't", "don't")
    left_low, right_low = left.lower(), right.lower()
    left_negated = any(marker in left_low for marker in negations)
    right_negated = any(marker in right_low for marker in negations)
    if left_negated == right_negated:
        return False
    return similarity(left, right) >= PARAPHRASE_THRESHOLD
