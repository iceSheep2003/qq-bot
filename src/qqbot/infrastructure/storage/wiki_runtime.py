"""Optional local Wiki runtime composition."""

from __future__ import annotations

import os
from pathlib import Path

from qqbot.agent.memory.wiki.cache import WikiContextCache
from qqbot.agent.memory.wiki.reader import ReadMode, WikiReader
from qqbot.infrastructure.storage.wiki_files import MarkdownWikiStore


class FileBackedWikiReader(WikiReader):
    """Reload published Markdown when its snapshot files change."""

    def __init__(self, root: Path, *, ttl_seconds: int):
        self._store = MarkdownWikiStore(root)
        self._signature = self._snapshot_signature()
        super().__init__(self._store.load(), cache=WikiContextCache(ttl_seconds=ttl_seconds))

    def _snapshot_signature(self) -> tuple[tuple[str, int, int], ...]:
        if not self._store.root.exists():
            return ()
        return tuple(
            (str(path.relative_to(self._store.root)), path.stat().st_mtime_ns, path.stat().st_size)
            for path in sorted(self._store.root.rglob("*.md"))
        )

    def _refresh(self) -> None:
        signature = self._snapshot_signature()
        if signature != self._signature:
            self.replace(self._store.load())
            self._signature = signature

    def read(self, scope: str, subject: str, query: str, *, mode: ReadMode = "selected", limit: int = 4) -> dict:
        self._refresh()
        return super().read(scope, subject, query, mode=mode, limit=limit)


def configured_wiki_reader() -> WikiReader | None:
    """Build the reader only when the deployer explicitly enables Wiki."""
    if os.getenv("BOT_WIKI_ENABLED", "false").strip().lower() != "true":
        return None
    root = Path(os.getenv("BOT_WIKI_ROOT", "./data/wiki"))
    try:
        ttl = max(0, int(os.getenv("BOT_WIKI_CONTEXT_TTL_SECONDS", "300")))
    except ValueError as exc:
        raise ValueError("BOT_WIKI_CONTEXT_TTL_SECONDS must be an integer") from exc
    return FileBackedWikiReader(root, ttl_seconds=ttl)

