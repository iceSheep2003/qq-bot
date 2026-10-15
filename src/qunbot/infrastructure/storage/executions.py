"""Durable execution receipts, not an automatic retry queue."""

from __future__ import annotations
import time

from qunbot.contracts.lifecycle import next_state
from qunbot.contracts.schema import ContractError, canonical
from qunbot.infrastructure.storage.base import SqliteRepository


def migrate(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS kernel_executions (
        scope TEXT NOT NULL,id TEXT NOT NULL,capability TEXT NOT NULL,
        state TEXT NOT NULL,input_hash TEXT NOT NULL,result TEXT,
        revision INTEGER NOT NULL DEFAULT 1,updated_at INTEGER NOT NULL,
        PRIMARY KEY(scope,id)
      );
      CREATE TABLE IF NOT EXISTS kernel_execution_events (
        scope TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
        event TEXT NOT NULL,state TEXT NOT NULL,time INTEGER NOT NULL,
        PRIMARY KEY(scope,id,revision)
      );
    """)


class ExecutionStore(SqliteRepository):
    def pending(self, scope, identity):
        with self.database._transaction_lock:
            row = self.db.execute(
                "SELECT state FROM kernel_executions WHERE scope=? AND id=?",
                (scope, identity),
            ).fetchone()
            return bool(row and row[0] == "executing")

    def begin(self, scope, identity, capability, input_hash):
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM kernel_executions WHERE scope=? AND id=?",
                (scope, identity),
            ).fetchone()
            if row:
                return dict(row)
            now = int(time.time() * 1000)
            self.db.execute(
                "INSERT INTO kernel_executions VALUES(?,?,?,?,?,?,?,?)",
                (scope, identity, capability, "executing", input_hash, None, 1, now),
            )
            self.db.execute(
                "INSERT INTO kernel_execution_events VALUES(?,?,?,?,?,?)",
                (scope, identity, 1, "execute", "executing", now),
            )
            return None

    def settle(self, scope, identity, state, result):
        events = {
            "succeeded": "confirmed_success",
            "failed": "confirmed_failure",
            "unknown": "uncertain_result",
        }
        with self.transaction():
            row = self.db.execute(
                "SELECT state,revision FROM kernel_executions WHERE scope=? AND id=?",
                (scope, identity),
            ).fetchone()
            if not row or row[0] != "executing":
                raise ContractError("Execution is not pending")
            # Pure lifecycle uses its declared trigger names; see lifecycle.py.
            if state not in {"succeeded", "failed", "unknown"}:
                raise ContractError("Invalid execution outcome")
            if next_state("execution", row[0], events[state]) != state:
                raise ContractError("Invalid lifecycle transition")
            revision = row[1] + 1
            now = int(time.time() * 1000)
            self.db.execute(
                "UPDATE kernel_executions SET state=?,result=?,revision=?,updated_at=? WHERE scope=? AND id=?",
                (state, canonical(result).decode(), revision, now, scope, identity),
            )
            self.db.execute(
                "INSERT INTO kernel_execution_events VALUES(?,?,?,?,?,?)",
                (scope, identity, revision, events[state], state, now),
            )

    def recover_uncertain(self):
        with self.transaction():
            rows = self.db.execute(
                "SELECT scope,id,revision FROM kernel_executions WHERE state='executing'"
            ).fetchall()
            now = int(time.time() * 1000)
            for row in rows:
                revision = row[2] + 1
                self.db.execute(
                    "UPDATE kernel_executions SET state='unknown',revision=?,updated_at=? WHERE scope=? AND id=?",
                    (revision, now, row[0], row[1]),
                )
                self.db.execute(
                    "INSERT INTO kernel_execution_events VALUES(?,?,?,?,?,?)",
                    (row[0], row[1], revision, "crash-recovery", "unknown", now),
                )
            return len(rows)
