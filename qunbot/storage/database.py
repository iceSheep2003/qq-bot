"""Shared SQLite connection; each repository owns its own schema migration."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import activity, conversation, jobs, memory, relationships


class SqliteDatabase:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        for module in (conversation, relationships, memory, activity, jobs):
            module.migrate(self.db)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()
