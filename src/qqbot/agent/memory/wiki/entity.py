"""Small, scope-aware Wiki entities.

The entity is the source model. Markdown is only a readable projection and
does not need to be loaded or parsed on every reply.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WikiFact:
    fact_id: str
    text: str
    topic: str = ""
    source_ids: tuple[str, ...] = ()
    confidence: float = 0.0

    def __post_init__(self) -> None:
        if not self.fact_id.strip() or not self.text.strip():
            raise ValueError("WikiFact requires an id and text")
        if not 0 <= self.confidence <= 1:
            raise ValueError("WikiFact confidence must be between 0 and 1")

    def matches(self, query: str) -> bool:
        needle = str(query or "").strip().casefold()
        return bool(needle) and (
            needle in self.text.casefold() or needle in self.topic.casefold()
        )


@dataclass(frozen=True)
class WikiEntity:
    scope: str
    entity_id: str
    name: str
    summary: str = ""
    facts: tuple[WikiFact, ...] = field(default_factory=tuple)
    version: str = ""

    def __post_init__(self) -> None:
        if not self.scope.strip() or not self.entity_id.strip() or not self.name.strip():
            raise ValueError("WikiEntity requires scope, entity_id and name")

    def preview(self) -> dict[str, str]:
        return {
            "scope": self.scope,
            "entity_id": self.entity_id,
            "name": self.name,
            "summary": self.summary,
            "version": self.version,
        }

    def select(self, query: str, *, limit: int = 4) -> tuple[WikiFact, ...]:
        matches = [fact for fact in self.facts if fact.matches(query)]
        matches.sort(key=lambda fact: (-fact.confidence, fact.fact_id))
        return tuple(matches[: max(0, int(limit))])
