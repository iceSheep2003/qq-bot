"""Optional local Wiki runtime composition."""

from __future__ import annotations

import os
from pathlib import Path

from qqbot.agent.memory.wiki.cache import WikiContextCache
from qqbot.agent.memory.wiki.reader import ReadMode, WikiReader
from qqbot.infrastructure.storage.wiki_files import MarkdownWikiStore
from qqbot.infrastructure.storage.wiki_migration import read_legacy_database
from qqbot.infrastructure.storage.wiki_sync import LegacyWikiSynchronizer


class FileBackedWikiReader(WikiReader):
    """Reload published Markdown when its snapshot files change."""

    def __init__(self, root: Path, *, ttl_seconds: int, synchronizer=None):
        self._store = MarkdownWikiStore(root)
        self._synchronizer = synchronizer
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
        if self._synchronizer is not None:
            self._synchronizer.refresh()
        self._refresh()
        return super().read(scope, subject, query, mode=mode, limit=limit)


def configured_wiki_reader(db_path: Path | None = None) -> WikiReader | None:
    """Build the reader only when the deployer explicitly enables Wiki."""
    if os.getenv("BOT_WIKI_ENABLED", "false").strip().lower() != "true":
        return None
    root = Path(os.getenv("BOT_WIKI_ROOT", "./data/wiki"))
    try:
        ttl = max(0, int(os.getenv("BOT_WIKI_CONTEXT_TTL_SECONDS", "300")))
    except ValueError as exc:
        raise ValueError("BOT_WIKI_CONTEXT_TTL_SECONDS must be an integer") from exc
    synchronizer = None
    if os.getenv("BOT_WIKI_SYNC_LEGACY", "false").strip().lower() == "true":
        if db_path is None:
            raise ValueError("BOT_WIKI_SYNC_LEGACY requires a database path")
        try:
            check_interval = max(0, int(os.getenv("BOT_WIKI_SYNC_CHECK_SECONDS", "60")))
        except ValueError as exc:
            raise ValueError("BOT_WIKI_SYNC_CHECK_SECONDS must be an integer") from exc
        synchronizer = LegacyWikiSynchronizer(
            lambda: read_legacy_database(db_path),
            root,
            check_interval_seconds=check_interval,
        )
    return FileBackedWikiReader(root, ttl_seconds=ttl, synchronizer=synchronizer)

