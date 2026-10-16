"""Optional, batched publication of accepted legacy memory into Wiki files."""

from __future__ import annotations

import time
import json
from dataclasses import asdict, dataclass
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
        self._published_records: dict[str, str] | None = None
        self._state_path = self._root / ".wiki-sync-state.json"
        self._load_state()

    def _load_state(self) -> None:
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                return
            self._last_source_version = state.get("source_version") or None
            published_at = state.get("published_at")
            self._last_published_at = int(published_at) if published_at is not None else None
            pending_since = state.get("pending_since")
            self._pending_since = int(pending_since) if pending_since is not None else None
            records = state.get("published_records")
            if isinstance(records, dict) and all(
                isinstance(key, str) and isinstance(value, str) for key, value in records.items()
            ):
                self._published_records = records
        except (OSError, ValueError, TypeError):
            return

    def _save_state(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "source_version": self._last_source_version,
                    "published_at": self._last_published_at,
                    "pending_since": self._pending_since,
                    "published_records": self._published_records,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._state_path)

    def _records(self, rows: dict[str, Any]) -> dict[str, str]:
        """Fingerprint only facts that actually contribute to generated pages."""
        pages = self._migration.projector.project(
            memories=rows.get("memories", ()), topics=rows.get("topics", ()),
            slang=rows.get("slang", ()),
        )
        result = {}
        for page in pages:
            for kind, values, identity in (
                ("fact", page.entries, "entry_id"),
                ("topic", page.topics, "topic_id"),
                ("slang", page.slang, "term"),
            ):
                for item in values:
                    key = published_version([page.page_id, kind, getattr(item, identity)])
                    result[key] = published_version(asdict(item))
        return result

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
        records = self._records(rows)
        source_version = published_version(records)
        changed = source_version != self._last_source_version
        if changed and self._pending_since is None:
            self._pending_since = now
            self._save_state()
        if not changed and self._pending_since is None and not force:
            return WikiSyncReport(False, False, source_version)
        baseline = self._published_records or {}
        pending = sum(records.get(key) != baseline.get(key) for key in records.keys() | baseline.keys())
        removed = any(key not in records for key in baseline)
        if pending == 0 and not force and self._published_records is not None:
            self._pending_since = None
            self._save_state()
            return WikiSyncReport(False, False, source_version)
        should_publish = self._policy.should_publish(
            now=now,
            last_published_at=self._last_published_at,
            pending_since=self._pending_since,
            pending_entries=pending,
            force=force or removed,
        )
        if not should_publish:
            return WikiSyncReport(True, False, source_version)
        migration = self._migration.run(rows, self._root, write=True)
        self._last_source_version = source_version
        self._published_records = records
        self._last_published_at = now
        self._pending_since = None
        self._save_state()
        return WikiSyncReport(True, True, source_version, migration)

