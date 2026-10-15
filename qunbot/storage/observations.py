"""Bounded durable advisory jobs; replay only explicitly idempotent consumers."""

from __future__ import annotations
import time

from ..contracts.schema import canonical
from .base import SqliteRepository


def migrate(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS observation_outbox(
        id TEXT PRIMARY KEY,handler_id TEXT NOT NULL,event_json TEXT NOT NULL,
        reply TEXT NOT NULL,draft_json TEXT,replayable INTEGER NOT NULL,
        status TEXT NOT NULL,attempts INTEGER NOT NULL,due_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS observation_due ON observation_outbox(status,due_at);
    """)


class ObservationOutbox(SqliteRepository):
    def enqueue(
        self, identity, handler_id, event, reply, draft, *, replayable, capacity=256
    ):
        if len(canonical(event)) + len(canonical(draft)) + len(reply.encode()) > 65536:
            return "oversized"
        with self.transaction():
            if self.db.execute(
                "SELECT 1 FROM observation_outbox WHERE id=?", (identity,)
            ).fetchone():
                return "duplicate"
            count = self.db.execute(
                "SELECT count(*) FROM observation_outbox WHERE status IN ('pending','running')"
            ).fetchone()[0]
            if count >= capacity:
                return "full"
            self.db.execute(
                "INSERT INTO observation_outbox VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    handler_id,
                    canonical(event).decode(),
                    reply,
                    canonical(draft).decode() if draft else None,
                    int(replayable),
                    "pending",
                    0,
                    int(time.time()),
                ),
            )
            return "queued"

    def recover(self):
        with self.transaction():
            self.db.execute(
                "UPDATE observation_outbox SET status=CASE WHEN replayable=1 THEN 'pending' ELSE 'unknown' END WHERE status='running'"
            )

    def lease(self, handler_ids):
        if not handler_ids:
            return None
        with self.transaction():
            placeholders = ",".join("?" for _ in handler_ids)
            row = self.db.execute(
                f"SELECT * FROM observation_outbox WHERE status='pending' AND due_at<=? AND handler_id IN ({placeholders}) ORDER BY due_at,id LIMIT 1",
                (int(time.time()), *handler_ids),
            ).fetchone()
            if not row:
                return None
            self.db.execute(
                "UPDATE observation_outbox SET status='running',attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
            return dict(row)

    def complete(self, identity):
        with self.transaction():
            self.db.execute(
                "UPDATE observation_outbox SET status='done' WHERE id=?", (identity,)
            )

    def failed(self, identity):
        with self.transaction():
            self.db.execute(
                "UPDATE observation_outbox SET status=CASE WHEN replayable=1 AND attempts<3 THEN 'pending' ELSE 'failed' END,due_at=? WHERE id=?",
                (int(time.time()) + 30, identity),
            )

    def pending(self):
        return int(
            self.db.execute(
                "SELECT count(*) FROM observation_outbox WHERE status IN ('pending','running')"
            ).fetchone()[0]
        )
