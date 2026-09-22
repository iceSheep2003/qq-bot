from __future__ import annotations

import sqlite3
import time

from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, user_id TEXT NOT NULL,
          content TEXT NOT NULL, importance INTEGER NOT NULL DEFAULT 1,
          source_event_id TEXT, created_at INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
          USING fts5(content, content='memories', content_rowid='id');
        CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memories BEGIN
          INSERT INTO memories_fts(rowid,content) VALUES(new.id,new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memories BEGIN
          INSERT INTO memories_fts(memories_fts,rowid,content)
          VALUES('delete',old.id,old.content);
        END;
    """)

class MemoryStore(SqliteRepository):

    def remember(
        self,
        scope: str,
        user_id: str,
        content: str,
        *,
        importance: int = 1,
        source_event_id: str | None = None,
    ) -> int:
        content = content.strip()[:1000]
        if not content:
            raise ValueError("empty memory")
        cursor = self.db.execute(
            "INSERT INTO memories(scope,user_id,content,importance,source_event_id,created_at) VALUES(?,?,?,?,?,?)",
            (
                scope,
                user_id,
                content,
                max(1, min(5, importance)),
                source_event_id,
                int(time.time()),
            ),
        )
        return int(cursor.lastrowid)

    def search_memories(self, scope: str, query: str, limit: int = 5) -> list[dict]:
        words = [w for w in query.replace('"', " ").split() if w]
        if not words:
            return []
        fts_query = " OR ".join('"' + w[:40] + '"' for w in words[:8])
        try:
            rows = self.db.execute(
                """SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts
                   JOIN memories m ON m.id=memories_fts.rowid
                   WHERE memories_fts MATCH ? AND m.scope=? ORDER BY rank, m.importance DESC LIMIT ?""",
                (fts_query, scope, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            # FTS tokenization may not match unsegmented Chinese phrases.
            rows = self.db.execute(
                "SELECT * FROM memories WHERE scope=? AND content LIKE ? ORDER BY importance DESC,created_at DESC LIMIT ?",
                (scope, f"%{query[:80]}%", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_memory(self, scope: str, user_id: str, content: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM memories WHERE scope=? AND user_id=? AND content=? LIMIT 1",
                (scope, user_id, content),
            ).fetchone()
            is not None
        )
