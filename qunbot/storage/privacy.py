"""Explicit cross-domain deletion; never exposed as a chat command."""

from .database import SqliteDatabase


def forget_user(database: SqliteDatabase, user_id: str) -> None:
    with database.transaction():
        for table in ("messages", "memories", "relations", "affection_events", "people"):
            database.db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
