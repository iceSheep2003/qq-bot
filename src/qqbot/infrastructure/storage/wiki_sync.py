"""Optional, batched publication of accepted legacy memory into Wiki files."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from qqbot.agent.memory.wiki.migration import WikiMigration, WikiMigrationReport
from qqbot.agent.memory.wiki.update_policy import WikiUpdatePolicy, published_version


@dataclass(frozen=True)
class WikiSyncReport:
    changed: bool
    published: bool
    source_version: str
    migration: WikiMigrationReport | None = None


class LegacyWikiSynchronizer:
    """Refresh Markdown only after the configured publication gate opens."""

    def __init__(
        self,
        snapshot_loader: Callable[[], dict[str, Any]],
        root: Path,
        *,
        policy: WikiUpdatePolicy | None = None,
        migration: WikiMigration | None = None,
        clock: Callable[[], int] = lambda: int(time.time()),
        check_interval_seconds: int = 0,
    ) -> None:
        self._load = snapshot_loader
        self._root = Path(root)
        self._policy = policy or WikiUpdatePolicy()
        self._migration = migration or WikiMigration()
        self._clock = clock
        self._check_interval_seconds = max(0, int(check_interval_seconds))
        self._last_checked_at: int | None = None
        self._last_source_version: str | None = None
        self._last_published_at: int | None = None
        self._pending_since: int | None = None

    def refresh(self, *, force: bool = False) -> WikiSyncReport:
        now = int(self._clock())
        if (
            not force
            and self._last_checked_at is not None
            and now - self._last_checked_at < self._check_interval_seconds
        ):
            return WikiSyncReport(False, False, self._last_source_version or "")
        self._last_checked_at = now
        rows = self._load()
        source_version = published_version(rows)
        changed = source_version != self._last_source_version
        if changed and self._pending_since is None:
            self._pending_since = now
        if not changed and self._pending_since is None:
            return WikiSyncReport(False, False, source_version)
        pending = sum(
            len(rows.get(name, ())) for name in ("memories", "topics", "slang")
        )
        should_publish = self._policy.should_publish(
            now=now,
            last_published_at=self._last_published_at,
            pending_since=self._pending_since,
            pending_entries=pending,
            force=force,
        )
        if not should_publish:
            return WikiSyncReport(True, False, source_version)
        migration = self._migration.run(rows, self._root, write=True)
        self._last_source_version = source_version
        self._last_published_at = now
        self._pending_since = None
        return WikiSyncReport(True, True, source_version, migration)

