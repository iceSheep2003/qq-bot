from __future__ import annotations

import time

from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS people (
          user_id TEXT PRIMARY KEY, nickname TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relations (
          group_id TEXT NOT NULL, user_id TEXT NOT NULL,
          affection INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
          PRIMARY KEY(group_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS affection_events (
          id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
          delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL
        );
    """)

class RelationshipsStore(SqliteRepository):
    def observe_user(self, user_id: str, nickname: str) -> None:
        self.db.execute(
            "INSERT INTO people(user_id,nickname,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "nickname=excluded.nickname,updated_at=excluded.updated_at",
            (user_id, nickname, int(time.time())),
        )

    def profile(self, group_id: str, user_id: str) -> dict:
        person = self.db.execute(
            "SELECT nickname FROM people WHERE user_id=?", (user_id,)
        ).fetchone()
        relation = self.db.execute(
            "SELECT affection FROM relations WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        return {
            "user_id": user_id,
            "nickname": person[0] if person else "",
            "affection": relation[0] if relation else 0,
        }

    def change_affection(
        self, group_id: str, user_id: str, delta: int, reason: str
    ) -> int:
        if not -3 <= delta <= 3 or delta == 0 or not reason.strip():
            raise ValueError("affection delta must be -3..3, excluding 0, with reason")
        now = int(time.time())
        with self.transaction():
            last = self.db.execute(
                "SELECT created_at FROM affection_events WHERE group_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                (group_id, user_id),
            ).fetchone()
            if last and now - last[0] < 3600:
                raise ValueError("affection cooldown active")
            current = self.profile(group_id, user_id)["affection"]
            value = max(-100, min(100, current + delta))
            self.db.execute(
                "INSERT INTO relations(group_id,user_id,affection,updated_at) VALUES(?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET affection=excluded.affection,updated_at=excluded.updated_at",
                (group_id, user_id, value, now),
            )
            self.db.execute(
                "INSERT INTO affection_events(group_id,user_id,delta,reason,created_at) VALUES(?,?,?,?,?)",
                (group_id, user_id, delta, reason[:240], now),
            )
        return value
