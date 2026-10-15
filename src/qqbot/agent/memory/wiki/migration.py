"""Explicit, repeatable local migration from legacy rows to Wiki Markdown."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .legacy import LegacyWikiProjector
from .writer import WikiWriter


@dataclass(frozen=True)
class WikiMigrationReport:
    pages: int
    entries: int
    topics: int
    slang: int
    written: int
    dry_run: bool


class WikiMigration:
    def __init__(self, *, projector: LegacyWikiProjector | None = None, writer: WikiWriter | None = None):
        self.projector = projector or LegacyWikiProjector()
        self.writer = writer or WikiWriter()

    def run(self, rows: dict[str, Any], root: Path, *, write: bool = False) -> WikiMigrationReport:
        pages = self.projector.project(
            memories=rows.get("memories", ()),
            topics=rows.get("topics", ()),
            slang=rows.get("slang", ()),
        )
        written = 0
        if write:
            for page in pages:
                self.writer.write(root, page)
                written += 1
        return WikiMigrationReport(
            pages=len(pages),
            entries=sum(len(page.entries) for page in pages),
            topics=sum(len(page.topics) for page in pages),
            slang=sum(len(page.slang) for page in pages),
            written=written,
            dry_run=not write,
        )


