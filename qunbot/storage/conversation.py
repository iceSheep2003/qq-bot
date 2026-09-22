from __future__ import annotations

import time

from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, scope TEXT NOT NULL,
          user_id TEXT NOT NULL, nickname TEXT NOT NULL, role TEXT NOT NULL,
          content TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_scope_time
          ON messages(scope, created_at DESC);
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
    ) -> bool:
        now = int(time.time())
        with self.transaction():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO messages(event_id,scope,user_id,nickname,role,content,created_at) VALUES(?,?,?,?,?,?,?)",
                (event_id, scope, user_id, nickname, role, content, now),
            )
        return cursor.rowcount > 0

    def recent(self, scope: str, limit: int = 24) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def message_count(self, scope: str) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM messages WHERE scope=? AND role='user'", (scope,)
            ).fetchone()[0]
        )
