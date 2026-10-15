"""Small link policy and Markdown rendering; no Obsidian dependency."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass


@dataclass(frozen=True)
class LinkCandidate:
    source_page_id: str
    target_page_id: str
    kind: str
    label: str
    confidence: float
    evidence_count: int = 1
    symmetric: bool = False

    def accepted(self, *, inferred_threshold: float = 0.75) -> bool:
        """Require stronger evidence for inferred links than direct mentions."""
        if self.confidence < 0 or self.confidence > 1 or self.evidence_count < 1:
            return False
        if self.symmetric:
            return self.confidence >= inferred_threshold and self.evidence_count >= 2
        return True


def reciprocal(candidate: LinkCandidate) -> LinkCandidate | None:
    """Return the reverse edge only for an explicitly symmetric relation."""
    if not candidate.symmetric or not candidate.accepted():
        return None
    return LinkCandidate(
        source_page_id=candidate.target_page_id,
        target_page_id=candidate.source_page_id,
        kind=candidate.kind,
        label=candidate.label,
        confidence=candidate.confidence,
        evidence_count=candidate.evidence_count,
        symmetric=True,
    )


def markdown_link(source_page_id: str, target_page_id: str, label: str) -> str:
    """Render a portable relative Markdown link between Wiki files."""
    source = f"{source_page_id}.md"
    target = f"{target_page_id}.md"
    source_dir = posixpath.dirname(source) or "."
    relative = posixpath.relpath(target, source_dir)
    return f"[{label or target_page_id}]({relative})"

