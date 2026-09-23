"""Explicit cross-domain deletion; never exposed as a chat command.

Every module here keeps its own tables, and new ones appear as features land.
A hardcoded list of table names would therefore go stale silently — the failure
mode being "we told the user their data was deleted and part of it was not".
So the tables are discovered from the schema instead, by the one rule that
actually defines the scope: a table that records a ``user_id`` is a table that
records something about that user.
"""

from __future__ import annotations

import logging

from .database import SqliteDatabase

log = logging.getLogger(__name__)


def _personal_tables(database: SqliteDatabase) -> list[str]:
    """Tables holding per-user rows, discovered from the live schema.

    FTS shadow tables are skipped: they mirror a base table that is already in
    the list, and the delete triggers keep them in sync.
    """
    names = [
        row[0]
        for row in database.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if not row[0].startswith("sqlite_")
        and not row[0].endswith(
            ("_fts", "_fts_data", "_fts_idx", "_fts_docsize", "_fts_config")
        )
    ]
    personal = []
    for name in names:
        columns = {row[1] for row in database.db.execute(f"PRAGMA table_info({name})")}
        if "user_id" in columns:
            personal.append(name)
    return sorted(personal)


def forget_user(database: SqliteDatabase, user_id: str) -> int:
    """Delete every row this user is the subject of. Returns rows removed.

    Scoped to ``user_id`` — content a person merely *appears in* (a group
    message the bot quoted, a memory about someone else) is a different
    question and deliberately not covered here.
    """
    removed = 0
    with database.transaction():
        for table in _personal_tables(database):
            cursor = database.db.execute(
                f"DELETE FROM {table} WHERE user_id=?", (user_id,)
            )
            removed += cursor.rowcount or 0
    if removed:
        log.info("Forgot user %s across %d rows", user_id, removed)
    return removed
