"""Group slang candidates: the one table group this feature owns.

Discovered by ``storage/database.py`` through its ``migrate`` hook, so adding
this file was enough — ``database.py`` was not edited.

Ownership boundary: this module reads the ``messages`` table (read-only, and
only because an extension has no handle on ``ConversationService``) and writes
only its own three tables. It never touches ``relationships``, ``memories``,
``jobs`` or any persona file. Affection belongs to relationships, memory
belongs to memory; this package stores none of either.
"""

from __future__ import annotations

import json
import time

from .base import SqliteRepository

# The only three states a term can be in. ``candidate`` is the default: seen
# often enough to be worth keeping, not yet trusted enough to use.
STATUS_CANDIDATE = "candidate"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_CANDIDATE, STATUS_APPROVED, STATUS_REJECTED)


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS slang_candidates (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          term TEXT NOT NULL,
          occurrences INTEGER NOT NULL DEFAULT 0,
          seen_users TEXT NOT NULL DEFAULT '[]',
          seen_days TEXT NOT NULL DEFAULT '[]',
          first_seen INTEGER NOT NULL,
          last_seen INTEGER NOT NULL,
          samples TEXT NOT NULL DEFAULT '[]',
          confidence REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'candidate',
          origin TEXT NOT NULL DEFAULT 'auto',
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          UNIQUE(scope, term)
        );
        CREATE INDEX IF NOT EXISTS slang_candidates_scope_confidence
          ON slang_candidates(scope, confidence DESC);

        CREATE TABLE IF NOT EXISTS slang_reviews (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          term TEXT NOT NULL,
          action TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS slang_reviews_lookup
          ON slang_reviews(scope, term, created_at DESC);

        CREATE TABLE IF NOT EXISTS slang_scan_state (
          scope TEXT PRIMARY KEY,
          last_message_id INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL
        );
    """)


def _dumps(values: list) -> str:
    return json.dumps(list(values), ensure_ascii=False)


class SlangStore(SqliteRepository):
    """Candidate evidence and review state, scoped per group."""

    def recent_messages(self, scope: str, limit: int = 200) -> list[dict]:
        """Read-only mirror of the conversation log, oldest first.

        Present so the package works with zero wiring. The intended path is
        ``ConversationService.conversations.recent``; see
        ``qunbot.extensions.slang.bind_service``.
        """
        rows = self.db.execute(
            "SELECT * FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?",
            (scope, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # -- candidates ---------------------------------------------------------

    def candidates(self, scope: str) -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT * FROM slang_candidates WHERE scope=?", (scope,)
        ).fetchall()
        return {row["term"]: dict(row) for row in rows}

    def top(self, scope: str, limit: int = 48) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM slang_candidates WHERE scope=? AND status!=?"
            " ORDER BY confidence DESC, occurrences DESC, term ASC LIMIT ?",
            (scope, STATUS_REJECTED, max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert(
        self,
        scope: str,
        term: str,
        *,
        occurrences: int,
        seen_users: list,
        seen_days: list,
        samples: list,
        confidence: float,
        now: int,
    ) -> None:
        """Write absolute counters, not increments.

        The caller merges with what is already stored, so a replayed window
        cannot double-count.
        """
        now = int(now)
        with self.transaction():
            self.db.execute(
                "INSERT INTO slang_candidates("
                " scope, term, occurrences, seen_users, seen_days, first_seen,"
                " last_seen, samples, confidence, status, origin,"
                " created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,'candidate','auto',?,?)"
                " ON CONFLICT(scope, term) DO UPDATE SET"
                " occurrences=excluded.occurrences,"
                " seen_users=excluded.seen_users,"
                " seen_days=excluded.seen_days,"
                " last_seen=excluded.last_seen,"
                " samples=excluded.samples,"
                " confidence=excluded.confidence,"
                " updated_at=excluded.updated_at",
                (
                    scope,
                    term,
                    int(occurrences),
                    _dumps(seen_users),
                    _dumps(seen_days),
                    now,
                    now,
                    _dumps(samples),
                    float(confidence),
                    now,
                    now,
                ),
            )

    def set_status(
        self, scope: str, term: str, status: str, *, origin: str = "review"
    ) -> bool:
        """Move a term between candidate/approved/rejected. Returns changed."""
        if status not in STATUSES:
            raise ValueError(f"unknown slang status: {status}")
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE slang_candidates SET status=?, origin=?, updated_at=?"
                " WHERE scope=? AND term=? AND status!=?",
                (status, origin, int(time.time()), scope, term, status),
            )
        return cursor.rowcount > 0

    def log_review(
        self, scope: str, term: str, action: str, note: str = ""
    ) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO slang_reviews(scope, term, action, note, created_at)"
                " VALUES(?,?,?,?,?)",
                (scope, term, action, note[:200], int(time.time())),
            )

    def reviews(self, scope: str | None = None, limit: int = 100) -> list[dict]:
        if scope is None:
            rows = self.db.execute(
                "SELECT * FROM slang_reviews ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM slang_reviews WHERE scope=? ORDER BY id DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, scope: str, keep: int) -> int:
        """Drop the least-supported unreviewed candidates. Reviewed rows stay.

        A deployer's decision is not eviction material: pruning silently
        dropping an approval would look like a bug in the review file.
        """
        keep = max(0, int(keep))
        with self.transaction():
            cursor = self.db.execute(
                "DELETE FROM slang_candidates WHERE scope=? AND status=? AND id NOT IN"
                " (SELECT id FROM slang_candidates WHERE scope=? AND status=?"
                "  ORDER BY confidence DESC, occurrences DESC, term ASC LIMIT ?)",
                (scope, STATUS_CANDIDATE, scope, STATUS_CANDIDATE, keep),
            )
        return cursor.rowcount

    # -- scan watermark -----------------------------------------------------

    def last_scan(self, scope: str) -> int:
        row = self.db.execute(
            "SELECT last_message_id FROM slang_scan_state WHERE scope=?", (scope,)
        ).fetchone()
        return int(row["last_message_id"]) if row else 0

    def advance_scan(self, scope: str, message_id: int) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO slang_scan_state(scope, last_message_id, updated_at)"
                " VALUES(?,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " last_message_id=MAX(last_message_id, excluded.last_message_id),"
                " updated_at=excluded.updated_at",
                (scope, int(message_id), int(time.time())),
            )

    def forget(self, scope: str, term: str | None = None) -> int:
        """Delete learned data for a scope, or one term in it."""
        with self.transaction():
            if term is None:
                cursor = self.db.execute(
                    "DELETE FROM slang_candidates WHERE scope=?", (scope,)
                )
            else:
                cursor = self.db.execute(
                    "DELETE FROM slang_candidates WHERE scope=? AND term=?",
                    (scope, term),
                )
        return cursor.rowcount
