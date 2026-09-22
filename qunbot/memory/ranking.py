"""Hybrid ranking over retrieval candidates.

The store returns candidates with raw signals (``lexical`` bm25, ``vector``
cosine). This module turns those signals plus importance, recency, access count,
subject match and confidence into one score and a human-readable reason list, so
every injected memory can be explained. Pure functions: no I/O.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .models import GROUP_SUBJECT, MemoryItem, RetrievalHit


@dataclass(frozen=True)
class RankingWeights:
    lexical: float = 1.0
    vector: float = 0.9
    importance: float = 0.35
    recency: float = 0.25
    access: float = 0.15
    subject: float = 0.4
    confidence: float = 0.4
    half_life_days: float = 30.0


DEFAULT_WEIGHTS = RankingWeights()


def _lexical_scores(candidates: list[dict]) -> dict[int, float]:
    """Min-max normalise bm25 (lower is better) across this candidate set."""
    ranks = [
        (row["id"], float(row["lexical"]))
        for row in candidates
        if row.get("lexical") is not None
    ]
    if not ranks:
        return {}
    best = min(rank for _, rank in ranks)
    worst = max(rank for _, rank in ranks)
    if math.isclose(best, worst):
        return {row_id: 1.0 for row_id, _ in ranks}
    return {row_id: (worst - rank) / (worst - best) for row_id, rank in ranks}


def _recency(created_at: int, now: int, half_life_days: float) -> float:
    age_days = max(0.0, (now - created_at) / 86400)
    return 0.5 ** (age_days / max(half_life_days, 0.5))


def rank(
    candidates: list[dict],
    *,
    now: int | None = None,
    subject_user_id: str | None = None,
    weights: RankingWeights = DEFAULT_WEIGHTS,
    limit: int = 4,
) -> list[RetrievalHit]:
    now = int(now or time.time())
    lexical = _lexical_scores(candidates)
    hits: list[RetrievalHit] = []
    for row in candidates:
        item = MemoryItem.from_row(row)
        reasons: list[str] = []
        score = 0.0

        lex = lexical.get(item.id)
        if lex is not None:
            score += weights.lexical * lex
            reasons.append(f"lexical={lex:.2f}")
        vec = row.get("vector")
        if vec is not None:
            score += weights.vector * float(vec)
            reasons.append(f"vector={float(vec):.2f}")
        score += weights.importance * (item.importance / 5.0)
        reasons.append(f"importance={item.importance}")
        recency = _recency(item.created_at, now, weights.half_life_days)
        score += weights.recency * recency
        reasons.append(f"age={(max(0, now - item.created_at) / 86400):.1f}d")
        if item.access_count:
            score += weights.access * min(1.0, math.log1p(item.access_count) / math.log1p(10))
            reasons.append(f"accesses={item.access_count}")
        if item.subject_user_id == GROUP_SUBJECT:
            reasons.append("group_shared")
        elif subject_user_id and item.subject_user_id == subject_user_id:
            score += weights.subject
            reasons.append("subject_match")
        score += weights.confidence * item.confidence
        reasons.append(f"confidence={item.confidence:.2f}")
        if item.status == "candidate":
            reasons.append("candidate")
        hits.append(RetrievalHit(item=item, score=score, reasons=tuple(reasons)))

    hits.sort(key=lambda hit: (-hit.score, -hit.item.created_at, hit.item.id))
    return hits[: max(0, int(limit))]
