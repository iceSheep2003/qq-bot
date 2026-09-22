"""Private persistence for the emotion package.

This store owns its own tables and, by default, its own database file. The
application's main ``Store`` is untouched, so the two schemas can evolve
independently and removing this package leaves no orphan tables behind.

Point ``BOT_MOOD_DB_PATH`` at the main database to keep everything in one
file; this module only ever creates its own two tables.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .state import DIMENSIONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS mood (
  scope TEXT PRIMARY KEY, valence REAL NOT NULL, energy REAL NOT NULL,
  stress REAL NOT NULL, interest REAL NOT NULL, sociability REAL NOT NULL,
  reason TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mood_events (
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL, deltas TEXT NOT NULL,
  reason TEXT NOT NULL, created_at INTEGER NOT NULL
);
"""

_COLUMNS = ", ".join(DIMENSIONS)
_PLACEHOLDERS = ", ".join("?" for _ in DIMENSIONS)
_UPDATES = ", ".join(f"{name}=excluded.{name}" for name in DIMENSIONS)


class EmotionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def load(self, scope: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM mood WHERE scope=?", (scope,)).fetchone()
        return dict(row) if row else {}

    def change(
        self,
        scope: str,
        values: dict[str, float],
        deltas: dict[str, int],
        reason: str,
    ) -> None:
        if not reason.strip():
            raise ValueError("mood change requires a reason")
        if not any(deltas.get(name) for name in DIMENSIONS):
            raise ValueError("mood change requires at least one nonzero delta")
        now = int(time.time())
        ordered = [float(values[name]) for name in DIMENSIONS]
        with self.transaction():
            self.db.execute(
                f"INSERT INTO mood(scope,{_COLUMNS},reason,updated_at) "
                f"VALUES(?,{_PLACEHOLDERS},?,?) "
                f"ON CONFLICT(scope) DO UPDATE SET {_UPDATES},"
                "reason=excluded.reason,updated_at=excluded.updated_at",
                (scope, *ordered, reason.strip()[:120], now),
            )
            self.db.execute(
                "INSERT INTO mood_events(scope,deltas,reason,created_at) VALUES(?,?,?,?)",
                (
                    scope,
                    json.dumps(
                        {k: v for k, v in deltas.items() if k in DIMENSIONS},
                        ensure_ascii=False,
                    ),
                    reason.strip()[:120],
                    now,
                ),
            )

    def event_count(self, scope: str) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM mood_events WHERE scope=?", (scope,)
            ).fetchone()[0]
        )

    def close(self) -> None:
        self.db.close()
