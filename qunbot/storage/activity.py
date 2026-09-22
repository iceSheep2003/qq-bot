from __future__ import annotations

import time
from typing import Any

from .base import SqliteRepository


def migrate(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS proactive_log (
        id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, content TEXT NOT NULL,
        created_at INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'random'
    )""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(proactive_log)")}
    if "source" not in columns:
        db.execute(
            "ALTER TABLE proactive_log ADD COLUMN source TEXT NOT NULL DEFAULT 'random'"
        )

class ActivityStore(SqliteRepository):

    def proactive_count_since(
        self, group_id: str, since: int, source: str | None = None
    ) -> int:
        """Count posts since ``since``. ``source=None`` counts every pool."""
        sql = "SELECT count(*) FROM proactive_log WHERE group_id=? AND created_at>=?"
        params: list[Any] = [group_id, since]
        if source is not None:
            sql += " AND source=?"
            params.append(source)
        return int(self.db.execute(sql, params).fetchone()[0])

    def last_proactive(self, group_id: str) -> int:
        row = self.db.execute(
            "SELECT created_at FROM proactive_log WHERE group_id=? ORDER BY id DESC LIMIT 1",
            (group_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def log_proactive(
        self, group_id: str, content: str, source: str = "random"
    ) -> None:
        self.db.execute(
            "INSERT INTO proactive_log(group_id,content,created_at,source) VALUES(?,?,?,?)",
            (group_id, content, int(time.time()), source),
        )
