"""Audit log for manual guidance and selected real-conversation examples.

This repository does not edit files. The persona extension may apply a
selected example via its bounded file manager; guidance remains manual-only.
"""

from __future__ import annotations

import hashlib
import json
import time

from .base import SqliteRepository

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_ACCEPTED, STATUS_REJECTED)


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS persona_proposals (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          suggestion TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'guidance',
          rationale TEXT NOT NULL DEFAULT '',
          evidence TEXT NOT NULL DEFAULT '[]',
          -- Which revision of the persona file this was written against. A
          -- suggestion judged against a persona that has since changed is
          -- worth flagging rather than silently presenting as current.
          persona_hash TEXT NOT NULL DEFAULT '',
          batch_size INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending',
          note TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL,
          decided_at INTEGER NOT NULL DEFAULT 0,
          UNIQUE(scope, suggestion)
        );
        CREATE INDEX IF NOT EXISTS persona_proposals_queue
          ON persona_proposals(scope, status, created_at DESC);

        CREATE TABLE IF NOT EXISTS persona_proposal_state (
          scope TEXT PRIMARY KEY,
          last_message_id INTEGER NOT NULL DEFAULT 0,
          last_run_at INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL
        );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(persona_proposals)")}
    if "kind" not in columns:
        db.execute("ALTER TABLE persona_proposals ADD COLUMN kind TEXT NOT NULL DEFAULT 'guidance'")


def persona_digest(text: str) -> str:
    """Which revision of the persona file a proposal was written against."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ProposalStore(SqliteRepository):
    """The review queue, its decisions, and the watermark that feeds it."""

    def record(
        self,
        scope: str,
        suggestions: list[dict],
        *,
        persona_hash: str = "",
        batch_size: int = 0,
        now: int | None = None,
    ) -> int:
        """Queue new suggestions. Returns how many were actually new.

        A repeat is dropped by the unique key rather than queued again: a
        deployer who rejected a suggestion should not have to reject it every
        six hours.
        """
        now = int(time.time()) if now is None else int(now)
        added = 0
        with self.transaction():
            for item in suggestions:
                suggestion = str(item.get("suggestion") or "").strip()
                kind = str(item.get("kind") or "guidance")
                if kind not in {"guidance", "example"}:
                    continue
                if not suggestion:
                    continue
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO persona_proposals"
                    "(scope,suggestion,kind,rationale,evidence,persona_hash,batch_size,"
                    " status,created_at) VALUES(?,?,?,?,?,?,?,'pending',?)",
                    (
                        scope,
                        suggestion[:300 if kind == "example" else 200],
                        kind,
                        str(item.get("rationale") or "").strip()[:120],
                        json.dumps(
                            list(item.get("evidence") or [])[:3], ensure_ascii=False
                        ),
                        str(persona_hash)[:32],
                        int(batch_size),
                        now,
                    ),
                )
                added += cursor.rowcount
        return added

    def pending(self, scope: str | None = None, limit: int = 50) -> list[dict]:
        return self._select(scope, STATUS_PENDING, limit)

    def accepted(self, scope: str | None = None, limit: int = 50) -> list[dict]:
        """What a deployer agreed with. The audit trail of the queue."""
        return self._select(scope, STATUS_ACCEPTED, limit)

    def _select(self, scope: str | None, status: str, limit: int) -> list[dict]:
        if scope:
            rows = self.db.execute(
                "SELECT * FROM persona_proposals WHERE scope=? AND status=?"
                " ORDER BY id DESC LIMIT ?",
                (scope, status, max(0, int(limit))),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM persona_proposals WHERE status=?"
                " ORDER BY id DESC LIMIT ?",
                (status, max(0, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def decide(
        self, proposal_id: int, status: str, *, note: str = "", now: int | None = None
    ) -> dict | None:
        """Record a decision. Returns the row, or None if there was no change."""
        if status not in STATUSES:
            raise ValueError(f"unknown proposal status: {status}")
        now = int(time.time()) if now is None else int(now)
        with self.transaction():
            self.db.execute(
                "UPDATE persona_proposals SET status=?, note=?, decided_at=?"
                " WHERE id=?",
                (status, str(note)[:200], now, int(proposal_id)),
            )
        row = self.db.execute(
            "SELECT * FROM persona_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        return dict(row) if row else None

    def detail(self, proposal_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM persona_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["evidence"] = _json_list(item.get("evidence"))
        return item

    def pending_suggestion(self, scope: str, suggestion: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM persona_proposals WHERE scope=? AND suggestion=? AND status='pending'",
            (scope, suggestion),
        ).fetchone()
        return dict(row) if row else None

    def stats(self, scope: str | None = None) -> dict:
        where, params = ("", ()) if not scope else (" WHERE scope=?", (scope,))
        rows = self.db.execute(
            "SELECT status, count(*) FROM persona_proposals" + where + " GROUP BY status",
            params,
        ).fetchall()
        counts = {status: 0 for status in STATUSES}
        counts.update({str(row[0]): int(row[1]) for row in rows})
        return counts

    # ------------------------------------------------------- scan watermark

    def last_scan(self, scope: str) -> tuple[int, int]:
        """``(last_message_id, last_run_at)`` for this scope."""
        row = self.db.execute(
            "SELECT last_message_id, last_run_at FROM persona_proposal_state"
            " WHERE scope=?",
            (scope,),
        ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def advance_scan(self, scope: str, message_id: int) -> None:
        """Remember how far the transcript has been read."""
        with self.transaction():
            self.db.execute(
                "INSERT INTO persona_proposal_state"
                "(scope,last_message_id,last_run_at,updated_at) VALUES(?,?,0,?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " last_message_id=MAX(last_message_id, excluded.last_message_id),"
                " updated_at=excluded.updated_at",
                (scope, int(message_id), int(time.time())),
            )

    def mark_run(self, scope: str, *, now: int | None = None) -> None:
        """Record the attempt, successful or not.

        This is the cost gate, so it fires either way: a model that is down or
        misconfigured must cost one attempt per interval, not one per scan.
        Kept apart from :meth:`advance_scan` for exactly that reason — a failed
        pass should be retried, but not immediately.
        """
        now = int(time.time()) if now is None else int(now)
        with self.transaction():
            self.db.execute(
                "INSERT INTO persona_proposal_state"
                "(scope,last_message_id,last_run_at,updated_at) VALUES(?,0,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " last_run_at=excluded.last_run_at,"
                " updated_at=excluded.updated_at",
                (scope, now, now),
            )

    def forget_scope(self, scope: str) -> int:
        removed = 0
        with self.transaction():
            for table in ("persona_proposals", "persona_proposal_state"):
                cursor = self.db.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
                removed += cursor.rowcount
        return removed


def _json_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
