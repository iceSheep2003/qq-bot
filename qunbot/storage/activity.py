from __future__ import annotations

import time
from datetime import datetime
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
    db.execute("""CREATE TABLE IF NOT EXISTS repeated_message_log (
        group_id TEXT NOT NULL, source_key TEXT NOT NULL, content_key TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        PRIMARY KEY(group_id, source_key)
    )""")
    repeat_columns = {row[1] for row in db.execute("PRAGMA table_info(repeated_message_log)")}
    if "content_key" not in repeat_columns:
        db.execute("ALTER TABLE repeated_message_log ADD COLUMN content_key TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX IF NOT EXISTS repeated_message_log_time ON repeated_message_log(group_id, created_at DESC)")

class ActivityStore(SqliteRepository):

    def claim_repeat(
        self, group_id: str, source_key: str, content_key: str, *, now: int,
        cooldown_seconds: int, content_cooldown_seconds: int, daily_limit: int,
    ) -> bool:
        """Atomically suppress a repeated source pair across bot restarts."""
        if daily_limit <= 0:
            return False
        day_start = int(datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp())
        with self.transaction():
            # Keep a bounded audit horizon while surviving the usual restarts.
            self.db.execute(
                "DELETE FROM repeated_message_log WHERE created_at<?", (now - 30 * 86400,)
            )
            if self.db.execute(
                "SELECT 1 FROM repeated_message_log WHERE group_id=? AND (source_key=? OR "
                "(content_key=? AND created_at>=?))",
                (group_id, source_key, content_key, now - max(0, content_cooldown_seconds)),
            ).fetchone():
                return False
            last = self.db.execute(
                "SELECT max(created_at) FROM repeated_message_log WHERE group_id=?",
                (group_id,),
            ).fetchone()[0]
            if last is not None and now - int(last) < max(0, cooldown_seconds):
                return False
            count = self.db.execute(
                "SELECT count(*) FROM repeated_message_log WHERE group_id=? AND created_at>=?",
                (group_id, day_start),
            ).fetchone()[0]
            if int(count) >= daily_limit:
                return False
            self.db.execute(
                "INSERT INTO repeated_message_log(group_id,source_key,content_key,created_at) VALUES(?,?,?,?)",
                (group_id, source_key, content_key, now),
            )
        return True

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

    def proactive_count_prefix_since(
        self, group_id: str, since: int, source_prefix: str
    ) -> int:
        row = self.db.execute(
            "SELECT count(*) FROM proactive_log WHERE group_id=? "
            "AND created_at>=? AND source LIKE ?",
            (group_id, since, source_prefix.replace("%", "\\%") + "%"),
        ).fetchone()
        return int(row[0])

    def log_proactive(
        self, group_id: str, content: str, source: str = "random"
    ) -> None:
        self.db.execute(
            "INSERT INTO proactive_log(group_id,content,created_at,source) VALUES(?,?,?,?)",
            (group_id, content, int(time.time()), source),
        )
