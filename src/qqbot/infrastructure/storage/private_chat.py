"""Durable admission adapter for daily private-chat participants."""

from datetime import date, timedelta

from qqbot.infrastructure.storage.base import SqliteRepository


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS private_chat_admissions (
        day TEXT NOT NULL, user_id TEXT NOT NULL, PRIMARY KEY(day, user_id)
    )""")


class PrivateChatQuota(SqliteRepository):
    def __init__(self, database, *, daily_limit=10):
        super().__init__(database)
        if not isinstance(daily_limit, int) or not 0 <= daily_limit <= 1000:
            raise ValueError("private daily user limit must be 0..1000")
        self.daily_limit = daily_limit

    def admit(self, user_id: str, day: str) -> bool:
        current = date.fromisoformat(day)
        if not user_id or not str(user_id).isdigit() or self.daily_limit == 0:
            return False
        with self.transaction():
            # Keep only the recent quota ledger; conversation retention is separate.
            self.db.execute("DELETE FROM private_chat_admissions WHERE day<?",
                            ((current - timedelta(days=30)).isoformat(),))
            if self.db.execute(
                "SELECT 1 FROM private_chat_admissions WHERE day=? AND user_id=?",
                (day, user_id),
            ).fetchone():
                return True
            count = self.db.execute(
                "SELECT count(*) FROM private_chat_admissions WHERE day=?", (day,)
            ).fetchone()[0]
            if count >= self.daily_limit:
                return False
            self.db.execute("INSERT INTO private_chat_admissions(day,user_id) VALUES(?,?)",
                            (day, user_id))
            return True
