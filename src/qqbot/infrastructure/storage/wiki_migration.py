"""SQLite read-only edge for the local Wiki migration command."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from qqbot.agent.memory.wiki.migration import WikiMigration


def _read_table(connection: sqlite3.Connection, table: str) -> list[dict]:
    try:
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def read_legacy_database(path: Path) -> dict[str, list[dict]]:
    """Read legacy tables without running migrations or opening write mode."""
    uri = f"file:{Path(path).resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {
            "memories": _read_table(connection, "memories"),
            "topics": _read_table(connection, "memory_topics"),
            "slang": _read_table(connection, "slang_candidates"),
        }
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Project legacy Kinna memory into Wiki Markdown")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--write", action="store_true", help="write Markdown; default is read-only preview")
    args = parser.parse_args(argv)
    report = WikiMigration().run(read_legacy_database(args.db), args.wiki_root, write=args.write)
    print(json.dumps(report.__dict__, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
