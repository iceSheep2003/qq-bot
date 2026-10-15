"""Entity-shaped Markdown Wiki values.

Markdown is the human-readable projection of these values.  The page identity
and entry provenance stay explicit so a reader can select a few claims without
loading the entire document into a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


WikiScope = Literal["group", "group_member", "private"]


@dataclass(frozen=True)
class WikiEntry:
    entry_id: str
    claim: str
    topic: str = ""
    source_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    mention_policy: Literal["direct", "soft_echo", "tone_only", "avoid_unless_asked"] = "soft_echo"
    lifecycle: Literal["draft", "reviewed", "verified", "disputed", "archived"] = "draft"

    def __post_init__(self) -> None:
        if not self.entry_id.strip() or not self.claim.strip():
            raise ValueError("WikiEntry requires an id and claim")
        if not 0 <= self.confidence <= 1:
            raise ValueError("WikiEntry confidence must be between 0 and 1")

    def matches(self, query: str) -> bool:
        needle = str(query or "").strip().casefold()
        if not needle:
            return False
        return needle in self.claim.casefold() or needle in self.topic.casefold()


@dataclass(frozen=True)
class WikiPage:
    page_id: str
    scope: WikiScope
    entity_id: str
    title: str
    summary: str
    entries: tuple[WikiEntry, ...] = field(default_factory=tuple)
    version: str = ""

    def __post_init__(self) -> None:
        if not self.page_id.strip() or not self.entity_id.strip():
            raise ValueError("WikiPage requires page_id and entity_id")
        if not self.title.strip():
            raise ValueError("WikiPage requires a title")

    def preview(self) -> dict[str, str]:
        """Return frontmatter-sized metadata, never the page body."""
        return {
            "page_id": self.page_id,
            "title": self.title,
            "summary": self.summary,
            "version": self.version,
        }

    def select(self, query: str, *, limit: int = 4) -> tuple[WikiEntry, ...]:
        """Select query-matching claims, preferring trusted active entries."""
        candidates = [entry for entry in self.entries if entry.matches(query)]
        candidates.sort(
            key=lambda entry: (
                entry.lifecycle == "archived",
                -entry.confidence,
                entry.entry_id,
            )
        )
        return tuple(candidates[: max(0, int(limit))])

