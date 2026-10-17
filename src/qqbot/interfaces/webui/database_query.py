"""Authenticated, bounded read-only projections over the running SQLite DB."""
from __future__ import annotations

import re
import sqlite3


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_LIMIT = 200


class DatabaseConsoleQuery:
    """Expose schema and SELECT results without handing the console a write port."""

    def __init__(self, database):
        self.database = database
        self.db = database.db

    @staticmethod
    def _limit(limit: int) -> int:
        if not 1 <= int(limit) <= _MAX_LIMIT:
            raise ValueError("limit 必须在 1–200 之间")
        return int(limit)

    def _table(self, table: str) -> str:
        name = str(table or "")
        if not _IDENTIFIER.fullmatch(name) or name.startswith("sqlite_"):
            raise ValueError("表名无效或不可访问")
        exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if exists is None:
            raise ValueError("表不存在")
        return name

    def schema(self) -> dict:
        with self.database._transaction_lock:
            tables = self.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            result = []
            for row in tables:
                name = str(row[0])
                columns = [
                    str(item[1])
                    for item in self.db.execute(f'PRAGMA table_info("{name}")').fetchall()
                ]
                count = int(self.db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                result.append({"name": name, "columns": columns, "row_count": count})
        return {"tables": result}

    def rows(self, table: str, *, limit: int = 100) -> dict:
        name = self._table(table)
        limit = self._limit(limit)
        with self.database._transaction_lock:
            cursor = self.db.execute(f'SELECT * FROM "{name}" LIMIT ?', (limit,))
            rows = [list(row) for row in cursor.fetchall()]
            total = int(self.db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            columns = [str(item[0]) for item in cursor.description or ()]
        return {"table": name, "columns": columns, "rows": rows, "total": total, "truncated": total > limit}

    def execute(self, sql: str, *, limit: int = 100) -> dict:
        limit = self._limit(limit)
        statement = str(sql or "").strip()
        if not statement or ";" in statement:
            raise ValueError("只允许执行一条不带分号的 SELECT 查询")
        if not re.match(r"(?is)^select\b", statement):
            raise ValueError("SQL 管理台只允许 SELECT 查询，写操作请使用对应的业务界面")
        with self.database._transaction_lock:
            try:
                cursor = self.db.execute(statement)
                columns = [str(item[0]) for item in cursor.description or ()]
                rows = [list(row) for row in cursor.fetchmany(limit + 1)]
            except sqlite3.Error as error:
                raise ValueError(f"SQL 查询失败：{error}") from error
        truncated = len(rows) > limit
        return {"columns": columns, "rows": rows[:limit], "truncated": truncated, "limit": limit}
