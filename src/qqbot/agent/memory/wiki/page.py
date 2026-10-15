"""Small, entity-shaped Wiki pages for distilled conversation memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


LinkKind = Literal[
    "mentions",
    "frequent_member",
    "likes_topic",
    "related_topic",
    "affection",
    "conflicts_with",
]


@dataclass(frozen=True)
class WikiEntry:
    entry_id: str
    claim: str
    topic: str = ""
    fact_type: str = "fact"
    persona_hint: str = ""
    mention_policy: Literal["direct", "soft_echo", "tone_only", "avoid_unless_asked"] = "soft_echo"
    source_event_id: str | None = None
    observed_at: int = 0
    updated_at: int = 0
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.entry_id.strip() or not self.claim.strip():
            raise ValueError("WikiEntry requires an id and claim")
        if not 0 <= self.confidence <= 1:
            raise ValueError("WikiEntry confidence must be between 0 and 1")

    def matches(self, query: str) -> bool:
        needle = str(query or "").strip().casefold()
        return bool(needle) and (
            needle in self.claim.casefold() or needle in self.topic.casefold()
        )


@dataclass(frozen=True)
class WikiRelation:
    relation_id: str
    source_entity_id: str
    target_entity_id: str
    kind: Literal["affection"] = "affection"
    score: float = 0.0
    observed_at: int = 0
    source_event_id: str | None = None

    def __post_init__(self) -> None:
        if not self.relation_id.strip():
            raise ValueError("WikiRelation requires an id")
        if not 0 <= self.score <= 1:
            raise ValueError("WikiRelation score must be between 0 and 1")


@dataclass(frozen=True)
class WikiTopic:
    topic_id: str
    name: str
    summary: str = ""
    aliases: tuple[str, ...] = ()
    mention_count: int = 0
    last_seen_at: int = 0

    def __post_init__(self) -> None:
        if not self.topic_id.strip() or not self.name.strip():
            raise ValueError("WikiTopic requires an id and name")
        if self.mention_count < 0:
            raise ValueError("WikiTopic mention_count cannot be negative")


@dataclass(frozen=True)
class WikiSlang:
    term: str
    definition: str
    examples: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    last_seen_at: int = 0

    def __post_init__(self) -> None:
        if not self.term.strip() or not self.definition.strip():
            raise ValueError("WikiSlang requires a term and definition")
        if not 0 <= self.confidence <= 1:
            raise ValueError("WikiSlang confidence must be between 0 and 1")


@dataclass(frozen=True)
class WikiLink:
    source_page_id: str
    target_page_id: str
    kind: LinkKind = "mentions"
    label: str = ""
    confidence: float = 1.0
    observed_at: int = 0

    def __post_init__(self) -> None:
        if not self.source_page_id.strip() or not self.target_page_id.strip():
            raise ValueError("WikiLink requires source and target page ids")
        if not 0 <= self.confidence <= 1:
            raise ValueError("WikiLink confidence must be between 0 and 1")


@dataclass(frozen=True)
class WikiPage:
    page_id: str
    scope: Literal["group", "group_member", "private"]
    entity_id: str
    title: str
    summary: str = ""
    entries: tuple[WikiEntry, ...] = field(default_factory=tuple)
    relations: tuple[WikiRelation, ...] = field(default_factory=tuple)
    topics: tuple[WikiTopic, ...] = field(default_factory=tuple)
    slang: tuple[WikiSlang, ...] = field(default_factory=tuple)
    links: tuple[WikiLink, ...] = field(default_factory=tuple)
    member_page_ids: tuple[str, ...] = ()
    version: str = ""

    def __post_init__(self) -> None:
        if not self.page_id.strip() or not self.entity_id.strip() or not self.title.strip():
            raise ValueError("WikiPage requires page_id, entity_id and title")

    def preview(self) -> dict[str, str]:
        return {
            "page_id": self.page_id,
            "title": self.title,
            "summary": self.summary,
            "version": self.version,
        }

    def select(self, query: str, *, limit: int = 4) -> tuple[WikiEntry, ...]:
        """Select recent matching facts; all older facts remain stored."""
        matches = [entry for entry in self.entries if entry.matches(query)]
        matches.sort(
            key=lambda entry: (-entry.observed_at, -entry.updated_at, entry.entry_id)
        )
        return tuple(matches[: max(0, int(limit))])
