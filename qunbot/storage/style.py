"""Expression samples for the opt-in style echo feature.

This table is the *only* place the style echo feature writes. A row exists only
for a user the deployer put on the allow-list; there is no code path that
records anyone else. Deletion is a first-class operation, not an afterthought:
``forget_user`` removes every row for one person across every scope, and
``forget_expired`` bounds how long a sample survives.

Nothing here knows what a "style" is — this module stores raw samples and the
extension derives an aggregate profile at read time. Keeping the derivation out
of storage means a future change to the analysis cannot leave stale, richer
data behind, and deleting a person deletes exactly what was collected.
"""

from __future__ import annotations

import hashlib
import time

from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS style_samples (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          user_id TEXT NOT NULL,
          content TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          UNIQUE(scope, user_id, fingerprint)
        );
        CREATE INDEX IF NOT EXISTS style_samples_subject
          ON style_samples(user_id, scope, id DESC);
        CREATE INDEX IF NOT EXISTS style_samples_time
          ON style_samples(created_at);
    """)


def fingerprint(content: str) -> str:
    """Stable, non-reversible dedupe key. Not a security hash — a shortening."""
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]


class StyleStore(SqliteRepository):
    """Samples belonging to explicitly allow-listed users, and their deletion."""

    def record(
        self, scope: str, user_id: str, content: str, now: int | None = None
    ) -> bool:
        """Store one sample. Returns False for a duplicate or empty text.

        The unique key is (scope, user, fingerprint), so re-scanning the same
        recent window on every poll is free rather than duplicating history.
        """
        text = (content or "").strip()
        if not text or not user_id:
            return False
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO style_samples"
            "(scope,user_id,content,fingerprint,created_at) VALUES(?,?,?,?,?)",
            (
                scope,
                user_id,
                text,
                fingerprint(text),
                int(time.time()) if now is None else int(now),
            ),
        )
        return cursor.rowcount > 0

    def samples(self, scope: str, user_id: str, limit: int = 40) -> list[str]:
        """Newest ``limit`` samples, returned oldest-first for stable analysis."""
        rows = self.db.execute(
            "SELECT content FROM style_samples WHERE scope=? AND user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (scope, user_id, int(limit)),
        ).fetchall()
        return [row[0] for row in reversed(rows)]

    def count(self, scope: str, user_id: str) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM style_samples WHERE scope=? AND user_id=?",
                (scope, user_id),
            ).fetchone()[0]
        )

    def total(self) -> int:
        """Every stored sample, whatever the subject. For tests and audits."""
        return int(self.db.execute("SELECT count(*) FROM style_samples").fetchone()[0])

    def subjects(self) -> list[dict]:
        """Who has samples, and how many. The consent roster, read-only."""
        rows = self.db.execute(
            "SELECT scope, user_id, count(*) AS n FROM style_samples "
            "GROUP BY scope, user_id ORDER BY user_id, scope"
        ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, scope: str, user_id: str, keep: int) -> int:
        """Trim one subject to the newest ``keep`` samples. Returns rows removed."""
        keep = max(0, int(keep))
        cursor = self.db.execute(
            "DELETE FROM style_samples WHERE scope=? AND user_id=? AND id NOT IN "
            "(SELECT id FROM style_samples WHERE scope=? AND user_id=? "
            " ORDER BY id DESC LIMIT ?)",
            (scope, user_id, scope, user_id, keep),
        )
        return cursor.rowcount

    def forget_expired(self, before: int) -> int:
        cursor = self.db.execute(
            "DELETE FROM style_samples WHERE created_at < ?", (int(before),)
        )
        return cursor.rowcount

    def forget_user(self, user_id: str) -> int:
        """Complete erasure for one person, in every group. Returns rows removed."""
        cursor = self.db.execute(
            "DELETE FROM style_samples WHERE user_id=?", (user_id,)
        )
        return cursor.rowcount

    def forget_scope(self, scope: str) -> int:
        cursor = self.db.execute(
            "DELETE FROM style_samples WHERE scope=?", (scope,)
        )
        return cursor.rowcount

    def forget_all(self) -> int:
        cursor = self.db.execute("DELETE FROM style_samples")
        return cursor.rowcount
