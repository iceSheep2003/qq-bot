"""Shared SQLite transaction boundary, not a repository facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database import SqliteDatabase


class SqliteRepository:
    def __init__(self, database: SqliteDatabase):
        self.db = database.db
        self.transaction = database.transaction
