from __future__ import annotations

import time
import json

from qqbot.infrastructure.storage.base import SqliteRepository
from qqbot.agent.conversation.parts import base_parts


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, scope TEXT NOT NULL,
          user_id TEXT NOT NULL, nickname TEXT NOT NULL, role TEXT NOT NULL,
          content TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_scope_time
          ON messages(scope, created_at DESC);
        CREATE TABLE IF NOT EXISTS message_metadata (
          event_id TEXT PRIMARY KEY, event_time INTEGER NOT NULL,
          origin TEXT NOT NULL, platform_message_id TEXT NOT NULL,
          reply_to_id TEXT NOT NULL,parts_json TEXT NOT NULL,
          parent_id TEXT NOT NULL,delivery_status TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS message_metadata_parent ON message_metadata(parent_id);
        CREATE TABLE IF NOT EXISTS conversation_projection_outbox (
          event_id TEXT PRIMARY KEY,completed INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS delivery_receipts (
          parent_id TEXT NOT NULL,part_index INTEGER NOT NULL,scope TEXT NOT NULL,
          kind TEXT NOT NULL,transcript TEXT NOT NULL,status TEXT NOT NULL,
          platform_message_id TEXT NOT NULL,updated_at INTEGER NOT NULL,
          PRIMARY KEY(parent_id,part_index)
        );
        CREATE TABLE IF NOT EXISTS knowledge_live_cursors(scope TEXT PRIMARY KEY,cursor INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS conversation_snapshots (
          scope TEXT PRIMARY KEY,covered_id INTEGER NOT NULL,last_id INTEGER NOT NULL,
          payload_json TEXT NOT NULL,created_at INTEGER NOT NULL
        );
    """)


class ConversationStore(SqliteRepository):
    def add_message(
        self,
        event_id: str,
        scope: str,
        user_id: str,
        nickname: str,
        role: str,
        content: str,
        *,
        metadata: dict | None = None,
    ) -> bool:
        now = int(time.time())
        with self.transaction():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO messages(event_id,scope,user_id,nickname,role,content,created_at) VALUES(?,?,?,?,?,?,?)",
                (event_id, scope, user_id, nickname, role, content, now),
            )
            if cursor.rowcount:
                details = metadata or {}
                self.db.execute(
                    "INSERT INTO message_metadata VALUES(?,?,?,?,?,?,?,?)",
                    (
                        event_id,
                        details.get("event_time", now * 1000),
                        details.get(
                            "origin", "agent" if role == "assistant" else "member"
                        ),
                        details.get("platform_message_id", ""),
                        details.get("reply_to_id", ""),
                        json.dumps(
                            details.get("parts", [{"kind": "text", "text": content}]),
                            ensure_ascii=False,
                        ),
                        details.get("parent_id", event_id),
                        details.get(
                            "delivery_status",
                            "sent" if role == "assistant" else "received",
                        ),
                    ),
                )
                self.db.execute(
                    "INSERT INTO conversation_projection_outbox(event_id) VALUES(?)",
                    (event_id,),
                )
        return cursor.rowcount > 0

    def add_event(self, event) -> bool:
        return self.add_message(
            event.event_id,
            event.scope,
            event.user_id,
            event.nickname,
            "user",
            event.text
            or (
                "[语音]"
                if any(p.kind == "audio" for p in event.inbound_parts)
                else "[图片]"
                if event.image_urls
                or any(p.kind == "image" for p in event.inbound_parts)
                else "[非文字消息]"
            ),
            metadata={
                "event_time": event.timestamp * 1000,
                "origin": "operator" if event.origin == "operator" else "member",
                "platform_message_id": event.platform_message_id,
                "reply_to_id": event.reply_to_message_id,
                "parts": list(event.ledger_parts) or base_parts(event),
            },
        )

    def attach_parts(self, event_id, scope, parts):
        """Enrich a received event without inserting a second dialogue turn."""
        with self.transaction():
            row = self.db.execute(
                "SELECT role FROM messages WHERE scope=? AND event_id=?",
                (scope, event_id),
            ).fetchone()
            if not row or row[0] != "user":
                raise ValueError("Inbound ledger event not found")
            self.db.execute(
                "UPDATE message_metadata SET parts_json=? WHERE event_id=?",
                (json.dumps(parts, ensure_ascii=False), event_id),
            )
            self.db.execute(
                "UPDATE conversation_projection_outbox SET completed=0,attempts=0 WHERE event_id=?",
                (event_id,),
            )

    def platform_event(self, scope, message_id):
        row = self.db.execute(
            "SELECT m.event_id FROM messages m JOIN message_metadata d ON d.event_id=m.event_id WHERE m.scope=? AND d.platform_message_id=? AND d.delivery_status IN ('received','sent') ORDER BY m.id DESC LIMIT 1",
            (scope, message_id),
        ).fetchone()
        return str(row[0]) if row else None

    def has_delivery(self, parent_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM message_metadata WHERE parent_id=? AND delivery_status='sent' LIMIT 1",
                (parent_id,),
            ).fetchone()
            is not None
        )

    def begin_part(self, parent_id, index, scope, kind, transcript):
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM delivery_receipts WHERE parent_id=? AND part_index=?",
                (parent_id, index),
            ).fetchone()
            if row:
                if (
                    row["scope"] != scope
                    or row["kind"] != kind
                    or row["transcript"] != transcript
                ):
                    raise ValueError("Delivery intent identity conflict")
                if row["status"] != "sent":
                    raise RuntimeError(
                        "Delivery outcome unknown; automatic resend refused"
                    )
                return {"message_id": row["platform_message_id"]}
            self.db.execute(
                "INSERT INTO delivery_receipts VALUES(?,?,?,?,?,?,?,?)",
                (
                    parent_id,
                    index,
                    scope,
                    kind,
                    transcript,
                    "executing",
                    "",
                    int(time.time() * 1000),
                ),
            )
            return None

    def settle_part(self, parent_id, receipt):
        with self.transaction():
            self.db.execute(
                "UPDATE delivery_receipts SET status=?,platform_message_id=?,updated_at=? WHERE parent_id=? AND part_index=?",
                (
                    receipt.status,
                    receipt.platform_message_id,
                    int(time.time() * 1000),
                    parent_id,
                    receipt.index,
                ),
            )

    def recover_parts(self):
        with self.transaction():
            return self.db.execute(
                "UPDATE delivery_receipts SET status='unknown' WHERE status='executing'"
            ).rowcount

    def actual_recent(self, scope, limit=100):
        rows = self.db.execute(
            "SELECT m.*,d.event_time,d.platform_message_id,d.reply_to_id,d.parts_json,d.delivery_status,d.origin FROM messages m LEFT JOIN message_metadata d ON d.event_id=m.event_id WHERE m.scope=? AND (d.origin IS NULL OR d.origin!='operator') AND (d.delivery_status IS NULL OR d.delivery_status IN ('received','sent')) ORDER BY m.id DESC LIMIT ?",
            (scope, max(1, min(240, limit))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def projection_batch(self, limit=50):
        rows = self.db.execute(
            "SELECT m.*,d.event_time,d.origin,d.platform_message_id,d.reply_to_id,d.parts_json,d.delivery_status FROM conversation_projection_outbox o JOIN messages m ON m.event_id=o.event_id LEFT JOIN message_metadata d ON d.event_id=m.event_id WHERE o.completed=0 AND o.attempts<3 ORDER BY m.id LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def ack_projection(self, event_id):
        with self.transaction():
            self.db.execute(
                "UPDATE conversation_projection_outbox SET completed=1 WHERE event_id=?",
                (event_id,),
            )

    def fail_projection(self, event_id):
        with self.transaction():
            self.db.execute(
                "UPDATE conversation_projection_outbox SET attempts=attempts+1 WHERE event_id=?",
                (event_id,),
            )

    def knowledge_cursor(self, scope):
        row = self.db.execute(
            "SELECT cursor FROM knowledge_live_cursors WHERE scope=?", (scope,)
        ).fetchone()
        return int(row[0]) if row else 0

    def advance_knowledge_cursor(self, scope, cursor, *, expected):
        with self.transaction():
            if self.knowledge_cursor(scope) != expected:
                raise ValueError("Knowledge cursor changed concurrently")
            self.db.execute(
                "INSERT INTO knowledge_live_cursors VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET cursor=excluded.cursor",
                (scope, cursor),
            )

    def continuity_snapshot(self, scope, *, max_age_seconds=5400):
        row = self.db.execute(
            "SELECT payload_json,covered_id,created_at FROM conversation_snapshots WHERE scope=?",
            (scope,),
        ).fetchone()
        if not row or row[2] < int(time.time()) - max_age_seconds:
            return None
        return {"covered_id": row[1], "message": json.loads(row[0])}

    def ledger_compaction_candidate(
        self, scope, *, max_messages, max_chars, tail_messages, current_event_id=""
    ):
        snapshot = self.continuity_snapshot(scope)
        covered = snapshot["covered_id"] if snapshot else 0
        # Compress the OLDEST uncovered partition, not the newest 240. Otherwise
        # a hot group silently loses the middle when the covered boundary jumps.
        rows = [
            dict(row)
            for row in self.db.execute(
                "SELECT m.*,d.event_time,d.platform_message_id,d.reply_to_id,d.parts_json,d.delivery_status,d.origin FROM messages m LEFT JOIN message_metadata d ON d.event_id=m.event_id "
                "WHERE m.scope=? AND m.id>? AND m.event_id!=? AND m.created_at>=? "
                "AND (d.origin IS NULL OR d.origin!='operator') AND (d.delivery_status IS NULL OR d.delivery_status IN ('received','sent')) ORDER BY m.id LIMIT 240",
                (scope, covered, current_event_id, int(time.time()) - 5400),
            )
        ]
        if (
            len(rows) <= max_messages
            and sum(len(r["content"]) for r in rows) <= max_chars
        ):
            return None
        head = rows[:-tail_messages]
        tail = rows[-tail_messages:]
        if not head:
            return None
        return {
            "rows": rows,
            "covered_id": head[-1]["id"],
            "expected_covered_id": covered,
            "last_id": rows[-1]["id"],
            "previous": snapshot["message"] if snapshot else None,
        }

    def commit_continuity(self, scope, candidate, snapshot):
        with self.transaction():
            row = self.db.execute(
                "SELECT covered_id FROM conversation_snapshots WHERE scope=?", (scope,)
            ).fetchone()
            # A stale snapshot may exist but have aged out of the live scene.
            existing = self.continuity_snapshot(scope)
            current = existing["covered_id"] if existing else 0
            if current != candidate["expected_covered_id"]:
                return False
            self.db.execute(
                "INSERT INTO conversation_snapshots VALUES(?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET covered_id=excluded.covered_id,last_id=excluded.last_id,payload_json=excluded.payload_json,created_at=excluded.created_at",
                (
                    scope,
                    candidate["covered_id"],
                    candidate["last_id"],
                    json.dumps(snapshot, ensure_ascii=False),
                    int(time.time()),
                ),
            )
            return True

    def recent(self, scope: str, limit: int = 24) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def after_id(self, scope: str, message_id: int, limit: int = 100) -> list[dict]:
        """Return an incremental user-message batch, oldest first."""
        rows = self.db.execute(
            "SELECT * FROM messages WHERE scope=? AND role='user' AND id>? "
            "ORDER BY id LIMIT ?",
            (scope, max(0, int(message_id)), max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def message_count(self, scope: str) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM messages WHERE scope=? AND role='user'", (scope,)
            ).fetchone()[0]
        )
