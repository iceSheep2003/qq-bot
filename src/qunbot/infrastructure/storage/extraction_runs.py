"""Live semantic extraction audit. Counts are processing results, not accuracy."""

from __future__ import annotations

import time
import uuid

from qunbot.infrastructure.storage.base import SqliteRepository


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ontology_extraction_runs (
          id TEXT PRIMARY KEY, scope TEXT NOT NULL, status TEXT NOT NULL,
          start_message_id INTEGER NOT NULL, end_message_id INTEGER NOT NULL,
          message_count INTEGER NOT NULL, proposed INTEGER NOT NULL DEFAULT 0,
          written INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0,
          strategy_version TEXT NOT NULL, started_at INTEGER NOT NULL,
          finished_at INTEGER, error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS ontology_extraction_scope
          ON ontology_extraction_runs(scope,started_at);
    """)


class ExtractionRunStore(SqliteRepository):
    def begin(self, scope, rows, version):
        identity = uuid.uuid4().hex
        with self.transaction():
            self.db.execute(
                "INSERT INTO ontology_extraction_runs(id,scope,status,start_message_id,end_message_id,message_count,strategy_version,started_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    scope,
                    "running",
                    rows[0]["id"],
                    rows[-1]["id"],
                    len(rows),
                    version,
                    int(time.time()),
                ),
            )
        return identity

    def finish(self, identity, result=None, *, error=""):
        result = result or {}
        with self.transaction():
            self.db.execute(
                "UPDATE ontology_extraction_runs SET status=?,proposed=?,written=?,rejected=?,finished_at=?,error=? WHERE id=? AND status='running'",
                (
                    "failed" if error else "succeeded",
                    result.get("proposed", 0),
                    result.get("written", 0),
                    result.get("rejected", 0),
                    int(time.time()),
                    error,
                    identity,
                ),
            )

    def recover(self):
        with self.transaction():
            self.db.execute(
                "UPDATE ontology_extraction_runs SET status='unknown',finished_at=?,error='进程中断；消息边界未确认，下一轮按持久游标幂等恢复。' WHERE status='running'",
                (int(time.time()),),
            )

    def list_runs(self, scope=None, limit=100):
        with self.database._transaction_lock:
            return [
                {**dict(row), "source": "ontology"}
                for row in self.db.execute(
                    "SELECT * FROM ontology_extraction_runs WHERE (? IS NULL OR scope=?) ORDER BY started_at DESC,id DESC LIMIT ?",
                    (scope, scope, max(1, min(200, limit))),
                )
            ]
