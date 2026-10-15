"""Durable native-group workflows and duplicate-notice admission."""

from __future__ import annotations
import json
import time
from dataclasses import asdict
from qqbot.contracts.lifecycle import next_state
from qqbot.contracts.schema import ContractError

from qqbot.infrastructure.storage.base import SqliteRepository


def migrate(db):
    db.executescript("""
      CREATE TABLE IF NOT EXISTS group_title_proposals(
        group_id TEXT PRIMARY KEY,body_json TEXT NOT NULL,status TEXT NOT NULL,
        revision INTEGER NOT NULL,updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS group_title_history(
        id INTEGER PRIMARY KEY,group_id TEXT NOT NULL,revision INTEGER NOT NULL,
        status TEXT NOT NULL,body_json TEXT NOT NULL,created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS group_notice_inbox(
        id TEXT PRIMARY KEY,group_id TEXT NOT NULL,user_id TEXT NOT NULL,
        kind TEXT NOT NULL,created_at INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS group_notice_admission ON group_notice_inbox(group_id,user_id,kind,created_at);
    """)


class TitleProposals(SqliteRepository):
    def __init__(self, database, factory):
        super().__init__(database)
        self.factory = factory

    def get(self, group_id):
        row = self.db.execute(
            "SELECT body_json,status FROM group_title_proposals WHERE group_id=?",
            (group_id,),
        ).fetchone()
        if not row or row[1] != "awaiting_consent":
            return None
        payload = json.loads(row[0])
        payload["voters"] = set(payload["voters"])
        return self.factory(**payload)

    def save(self, proposal, status="awaiting_consent"):
        body = asdict(proposal)
        body["voters"] = sorted(proposal.voters)
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
        now = int(time.time() * 1000)
        with self.transaction():
            old = self.db.execute(
                "SELECT body_json,status,revision FROM group_title_proposals WHERE group_id=?",
                (proposal.group_id,),
            ).fetchone()
            if old and old[0] == encoded and old[1] == status:
                return
            if old and old[1] != status:
                events = {
                    "awaiting_consent": "published",
                    "approved": "qualified_approval",
                    "executing": "authorize",
                    "succeeded": "confirmed_success",
                    "failed": "confirmed_failure",
                    "unknown": "uncertain_result",
                    "rejected": "reject",
                    "expired": "expire",
                    "cancelled": "cancel",
                }
                is_new = status == "proposed" and old[1] in {
                    "succeeded",
                    "failed",
                    "unknown",
                    "rejected",
                    "expired",
                    "cancelled",
                }
                if not is_new:
                    if status == "failed" and old[1] == "proposed":
                        status = "cancelled"
                    elif (
                        next_state("proposal", old[1], events.get(status, "")) != status
                    ):
                        raise ContractError("Invalid proposal transition")
            elif not old and status != "proposed":
                raise ContractError("Proposal must start proposed")
            revision = old[2] + 1 if old else 1
            self.db.execute(
                "INSERT INTO group_title_proposals VALUES(?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET body_json=excluded.body_json,status=excluded.status,revision=excluded.revision,updated_at=excluded.updated_at",
                (proposal.group_id, encoded, status, revision, now),
            )
            self.db.execute(
                "INSERT INTO group_title_history(group_id,revision,status,body_json,created_at) VALUES(?,?,?,?,?)",
                (proposal.group_id, revision, status, encoded, now),
            )

    def finish(self, group_id, status):
        proposal = self.get(group_id)
        if proposal:
            self.save(proposal, status)

    def list(self, group_id):
        row = self.db.execute(
            "SELECT * FROM group_title_proposals WHERE group_id=?", (group_id,)
        ).fetchone()
        return [dict(row)] if row else []

    def reconcile_interrupted(self):
        with self.transaction():
            self.db.execute(
                "UPDATE group_title_proposals SET status='unknown' WHERE status='executing'"
            )


class NoticeInbox(SqliteRepository):
    def accept(self, identity, group_id, user_id, kind, *, coalesce_seconds=0):
        now = int(time.time())
        with self.transaction():
            if (
                coalesce_seconds
                and self.db.execute(
                    "SELECT 1 FROM group_notice_inbox WHERE group_id=? AND user_id=? AND kind=? AND created_at>=? LIMIT 1",
                    (group_id, user_id, kind, now - coalesce_seconds),
                ).fetchone()
            ):
                return False
            return (
                self.db.execute(
                    "INSERT OR IGNORE INTO group_notice_inbox VALUES(?,?,?,?,?)",
                    (identity, group_id, user_id, kind, now),
                ).rowcount
                > 0
            )
