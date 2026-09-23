"""Private persistence for the emotion package.

This store owns its own tables and, by default, its own database file. The
application's main ``Store`` is untouched, so the two schemas can evolve
independently and removing this package leaves no orphan tables behind.

Point ``BOT_MOOD_DB_PATH`` at the main database to keep everything in one
file; this module only ever creates its own three tables, and touches no table
it did not create.

Three tables, three jobs: ``mood`` is the latest state per scope, ``mood_events``
is the append-only log replay folds, and ``mood_observed`` is the idempotency
ledger that makes a redelivered turn a no-op at *this* layer, independently of
whatever the caller already deduplicated.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .state import DIMENSIONS, MoodEvent

log = logging.getLogger(__name__)

# How many applied-turn keys are kept per scope. The window only has to outlive
# a redelivery or a restart replay, so it is generous but not unbounded.
DEDUPE_LIMIT = 4096

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
CREATE TABLE IF NOT EXISTS mood_observed (
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL, dedupe_key TEXT NOT NULL,
  created_at INTEGER NOT NULL, UNIQUE(scope, dedupe_key)
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
        self._closed = False

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
        *,
        dedupe_key: str | None = None,
        now: int | None = None,
    ) -> bool:
        """Write one mood change and log it. Returns False if already applied.

        ``dedupe_key`` names the turn this change came from. The claim and the
        write share a transaction, so two concurrent redeliveries of the same
        turn cannot both land: the loser's INSERT OR IGNORE reports no new row
        and it writes nothing. Claiming at write time rather than before the
        model call also means a *failed* assessment (malformed verdict, model
        error) leaves the key free, so a genuine retry still gets its chance —
        the log therefore records what was applied, never what was attempted.

        ``now`` is the instant the caller's policy used. Passing it keeps
        ``mood.updated_at`` and ``mood_events.created_at`` equal, which is what
        lets :func:`qunbot.emotion.state.replay` reproduce the stored values
        exactly instead of off by one decay step.
        """
        if not reason.strip():
            raise ValueError("mood change requires a reason")
        if not any(deltas.get(name) for name in DIMENSIONS):
            raise ValueError("mood change requires at least one nonzero delta")
        stamp = int(time.time()) if now is None else int(now)
        ordered = [float(values[name]) for name in DIMENSIONS]
        with self.transaction():
            if dedupe_key is not None:
                claimed = self.db.execute(
                    "INSERT OR IGNORE INTO mood_observed(scope,dedupe_key,created_at)"
                    " VALUES(?,?,?)",
                    (scope, dedupe_key, stamp),
                ).rowcount
                if not claimed:
                    log.debug("mood change for %s already applied", dedupe_key)
                    return False
            self.db.execute(
                f"INSERT INTO mood(scope,{_COLUMNS},reason,updated_at) "
                f"VALUES(?,{_PLACEHOLDERS},?,?) "
                f"ON CONFLICT(scope) DO UPDATE SET {_UPDATES},"
                "reason=excluded.reason,updated_at=excluded.updated_at",
                (scope, *ordered, reason.strip()[:120], stamp),
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
                    stamp,
                ),
            )
            if dedupe_key is not None:
                self._prune(scope)
        return True

    def seen(self, scope: str, dedupe_key: str) -> bool:
        """Whether this turn has already been applied. A cheap pre-model check."""
        row = self.db.execute(
            "SELECT 1 FROM mood_observed WHERE scope=? AND dedupe_key=?",
            (scope, dedupe_key),
        ).fetchone()
        return row is not None

    def _prune(self, scope: str) -> None:
        """Drop ledger entries older than the newest ``DEDUPE_LIMIT``."""
        self.db.execute(
            "DELETE FROM mood_observed WHERE scope=? AND id <= "
            "(SELECT max(id) FROM mood_observed WHERE scope=?) - ?",
            (scope, scope, DEDUPE_LIMIT),
        )

    def events(self, scope: str) -> tuple[MoodEvent, ...]:
        """The append-only log, in the order the changes were applied."""
        rows = self.db.execute(
            "SELECT deltas, reason, created_at FROM mood_events"
            " WHERE scope=? ORDER BY id",
            (scope,),
        ).fetchall()
        events: list[MoodEvent] = []
        for row in rows:
            deltas: dict[str, int] = {}
            try:
                decoded = json.loads(row["deltas"])
            except (json.JSONDecodeError, TypeError):
                log.warning("unreadable mood event for %s; skipped", scope)
                continue
            if isinstance(decoded, dict):
                for name, value in decoded.items():
                    if name in DIMENSIONS:
                        try:
                            deltas[name] = int(value)
                        except (TypeError, ValueError):
                            continue
            events.append(
                MoodEvent(
                    at=int(row["created_at"]),
                    deltas=deltas,
                    reason=str(row["reason"] or ""),
                )
            )
        return tuple(events)

    def event_count(self, scope: str) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM mood_events WHERE scope=?", (scope,)
            ).fetchone()[0]
        )

    def close(self) -> None:
        """Idempotent: shutdown and restart paths both close the same handle."""
        if self._closed:
            return
        self._closed = True
        self.db.close()
