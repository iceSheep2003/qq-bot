"""Shared SQLite transaction boundary, not a repository facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qunbot.infrastructure.storage.database import SqliteDatabase


class SqliteRepository:
    def __init__(self, database: SqliteDatabase):
        self.database = database
        self.db = database.db
        self.transaction = database.transaction
