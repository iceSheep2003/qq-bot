"""Memory domain objects.

Pure data: no storage, no model client, no network. ``MemoryItem`` mirrors the
``memories`` row; ``RetrievalHit`` carries the explanation for one injection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Subject used for facts that belong to the whole conversation, not a person.
GROUP_SUBJECT = "_group_"

FACT_TYPES = (
    "fact", "preference", "relation", "event", "boundary", "open_loop", "promise"
)

MENTION_POLICIES = ("direct", "soft_echo", "tone_only", "avoid_unless_asked")

STATUS_ACTIVE = "active"
STATUS_CANDIDATE = "candidate"
STATUS_ARCHIVED = "archived"
STATUS_EXPIRED = "expired"
STATUS_SUPERSEDED = "superseded"

VISIBILITY_GROUP = "group"
VISIBILITY_PERSONAL = "personal"

#: A fact observed with less confidence than this waits as a candidate.
CANDIDATE_CONFIDENCE = 0.6

OUTCOME_INSERTED = "inserted"
OUTCOME_REINFORCED = "reinforced"
OUTCOME_SUPERSEDED = "superseded"


@dataclass(frozen=True)
class MemoryItem:
    id: int
    scope: str
    subject_user_id: str
    content: str
    persona_summary: str = ""
    mention_policy: str = "soft_echo"
    fact_type: str = "fact"
    confidence: float = 1.0
    importance: int = 1
    status: str = STATUS_ACTIVE
    visibility: str = VISIBILITY_GROUP
    source_event_id: str | None = None
    origin_user_id: str | None = None
    expires_at: int | None = None
    last_accessed_at: int | None = None
    access_count: int = 0
    created_at: int = 0
    updated_at: int = 0

    @classmethod
    def from_row(cls, row: Any) -> "MemoryItem":
        if isinstance(row, Mapping):
            def get(key, default=None):
                return row.get(key, default)
        else:
            keys = set(row.keys())

            def get(key, default=None):
                return row[key] if key in keys else default

        return cls(
            id=int(get("id", 0) or 0),
            scope=str(get("scope", "") or ""),
            subject_user_id=str(get("user_id", GROUP_SUBJECT) or GROUP_SUBJECT),
            content=str(get("content", "") or ""),
            persona_summary=str(get("persona_summary", "") or ""),
            mention_policy=str(get("mention_policy", "soft_echo") or "soft_echo"),
            fact_type=str(get("fact_type", "fact") or "fact"),
            confidence=float(get("confidence", 1.0) or 0.0),
            importance=int(get("importance", 1) or 1),
            status=str(get("status", STATUS_ACTIVE) or STATUS_ACTIVE),
            visibility=str(get("visibility", VISIBILITY_GROUP) or VISIBILITY_GROUP),
            source_event_id=get("source_event_id"),
            origin_user_id=get("origin_user_id"),
            expires_at=get("expires_at"),
            last_accessed_at=get("last_accessed_at"),
            access_count=int(get("access_count", 0) or 0),
            created_at=int(get("created_at", 0) or 0),
            updated_at=int(get("updated_at", 0) or 0),
        )

    def is_personal(self) -> bool:
        return self.subject_user_id != GROUP_SUBJECT

    def describe(self) -> str:
        return (
            f"id={self.id} scope={self.scope} subject={self.subject_user_id} "
            f"status={self.status} confidence={self.confidence:.2f} "
            f"source_event={self.source_event_id or 'none'}"
        )


@dataclass(frozen=True)
class RetrievalHit:
    """One memory chosen for injection plus the reason it was chosen."""

    item: MemoryItem
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def explain(self) -> str:
        return (
            f"memory {self.item.id} from {self.item.source_event_id or 'unknown source'} "
            f"score={self.score:.3f} because {'; '.join(self.reasons) or 'no signal'}"
        )


@dataclass(frozen=True)
class MemoryEvidence:
    """One source observation supporting a memory proposal."""

    event_id: str
    user_id: str
    excerpt: str
    observed_at: int = 0
    weight: float = 1.0


@dataclass(frozen=True)
class MemoryProposal:
    """Model output before persistence policy accepts or rejects it."""

    subject_user_id: str
    content: str
    persona_summary: str = ""
    mention_policy: str = "soft_echo"
    fact_type: str = "fact"
    confidence: float = 0.65
    importance: int = 1
    visibility: str = VISIBILITY_PERSONAL
    topic: str = ""
    evidence_event_ids: tuple[str, ...] = ()
    supersedes: bool = False
