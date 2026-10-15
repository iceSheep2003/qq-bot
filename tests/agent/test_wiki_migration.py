from __future__ import annotations

import sqlite3

from qqbot.agent.memory.wiki.migration import WikiMigration
from qqbot.infrastructure.storage.wiki_migration import read_legacy_database


def rows():
    return {
        "memories": [
            {"id": 1, "scope": "group:1", "user_id": "_group_", "content": "群里常聊系统", "status": "active", "created_at": 1, "updated_at": 1, "confidence": 0.8},
        ],
        "topics": [],
        "slang": [],
    }


def test_dry_run_reports_pages_without_writing(tmp_path):
    report = WikiMigration().run(rows(), tmp_path, write=False)

    assert report.pages == 1
    assert report.entries == 1
    assert report.written == 0
    assert not list(tmp_path.rglob("*.md"))


def test_write_is_repeatable_and_reports_written_pages(tmp_path):
    migration = WikiMigration()
    first = migration.run(rows(), tmp_path, write=True)
    content = (tmp_path / "groups/1.md").read_text(encoding="utf-8")
    second = migration.run(rows(), tmp_path, write=True)

    assert first.written == second.written == 1
    assert (tmp_path / "groups/1.md").read_text(encoding="utf-8") == content


def test_database_reader_uses_read_only_mode_and_does_not_migrate(tmp_path):
    db = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE memories (id INTEGER, scope TEXT, user_id TEXT, content TEXT, status TEXT, created_at INTEGER, updated_at INTEGER, confidence REAL)")
    connection.execute("INSERT INTO memories VALUES (1, 'group:1', '_group_', '旧事实', 'active', 1, 1, 0.8)")
    connection.commit()
    connection.close()

    rows = read_legacy_database(db)

    assert len(rows["memories"]) == 1
    check = sqlite3.connect(db)
    assert check.execute("SELECT name FROM sqlite_master WHERE name='memories'").fetchone()
    check.close()
