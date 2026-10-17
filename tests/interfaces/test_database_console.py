from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qqbot.infrastructure.storage.database import SqliteDatabase
from qqbot.interfaces.webui.database_query import DatabaseConsoleQuery


@pytest.fixture
def query(tmp_path: Path):
    database = SqliteDatabase(tmp_path / "console.db")
    database.db.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    database.db.executemany("INSERT INTO demo(name) VALUES (?)", [("one",), ("two",)])
    yield DatabaseConsoleQuery(database)
    database.close()


def test_schema_and_table_rows_use_real_database(query):
    schema = query.schema()
    demo = next(table for table in schema["tables"] if table["name"] == "demo")
    assert demo["columns"] == ["id", "name"]
    assert demo["row_count"] == 2
    rows = query.rows("demo", limit=1)
    assert rows["columns"] == ["id", "name"]
    assert rows["rows"] == [[1, "one"]]
    assert rows["total"] == 2


def test_read_only_query_allows_bounded_select_and_rejects_writes(query):
    result = query.execute("SELECT id, name FROM demo ORDER BY id", limit=1)
    assert result["rows"] == [[1, "one"]]
    assert result["truncated"] is True
    for sql in ("DELETE FROM demo", "UPDATE demo SET name='x'", "INSERT INTO demo(name) VALUES ('x')", "PRAGMA user_version", "SELECT 1; SELECT 2"):
        with pytest.raises(ValueError):
            query.execute(sql)
    assert query.rows("demo")["total"] == 2


def test_sql_identifier_and_bounds_are_strict(query):
    for table in ("demo; DROP TABLE demo", "sqlite_master", "../demo", ""):
        with pytest.raises(ValueError):
            query.rows(table)
    with pytest.raises(ValueError):
        query.rows("demo", limit=0)
    with pytest.raises(ValueError):
        query.rows("demo", limit=201)
    with pytest.raises(ValueError):
        query.execute("SELECT * FROM demo", limit=0)
    with pytest.raises(ValueError):
        query.execute("SELECT * FROM demo", limit=201)


def test_query_reports_database_errors_without_mutating_state(query):
    with pytest.raises(ValueError, match="SQL 查询失败"):
        query.execute("SELECT missing FROM demo")
    assert query.rows("demo")["total"] == 2
