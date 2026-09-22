from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Store:
    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, scope TEXT NOT NULL,
          user_id TEXT NOT NULL, nickname TEXT NOT NULL, role TEXT NOT NULL,
          content TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS messages_scope_time ON messages(scope, created_at DESC);
        CREATE TABLE IF NOT EXISTS people (
          user_id TEXT PRIMARY KEY, nickname TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relations (
          group_id TEXT NOT NULL, user_id TEXT NOT NULL, affection INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL, PRIMARY KEY(group_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS affection_events (
          id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
          delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memories (
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, user_id TEXT NOT NULL,
          content TEXT NOT NULL, importance INTEGER NOT NULL DEFAULT 1,
          source_event_id TEXT, created_at INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, content='memories', content_rowid='id');
        CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memories BEGIN
          INSERT INTO memories_fts(rowid,content) VALUES(new.id,new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memories BEGIN
          INSERT INTO memories_fts(memories_fts,rowid,content) VALUES('delete',old.id,old.content);
        END;
        CREATE TABLE IF NOT EXISTS proactive_log (
          id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, content TEXT NOT NULL,
          created_at INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'random'
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, schedule_kind TEXT NOT NULL,
          schedule_value TEXT NOT NULL, prompt TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          next_run INTEGER NOT NULL, created_by TEXT NOT NULL, created_at INTEGER NOT NULL,
          config_key TEXT UNIQUE, action TEXT NOT NULL DEFAULT 'chat'
        );
        CREATE TABLE IF NOT EXISTS job_runs (
          id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, status TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '', started_at INTEGER NOT NULL, finished_at INTEGER
        );
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "config_key" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN config_key TEXT")
            self.db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_config_key ON jobs(config_key)"
            )
        if "action" not in columns:
            self.db.execute(
                "ALTER TABLE jobs ADD COLUMN action TEXT NOT NULL DEFAULT 'chat'"
            )
        log_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(proactive_log)")
        }
        if "source" not in log_columns:
            # Rows written before quotas were split were all random proactive.
            self.db.execute(
                "ALTER TABLE proactive_log ADD COLUMN source TEXT NOT NULL DEFAULT 'random'"
            )

    def add_message(
        self,
        event_id: str,
        scope: str,
        user_id: str,
        nickname: str,
        role: str,
        content: str,
    ) -> bool:
        now = int(time.time())
        with self.transaction():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO messages(event_id,scope,user_id,nickname,role,content,created_at) VALUES(?,?,?,?,?,?,?)",
                (event_id, scope, user_id, nickname, role, content, now),
            )
            if cursor.rowcount and role == "user":
                self.db.execute(
                    "INSERT INTO people(user_id,nickname,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname,updated_at=excluded.updated_at",
                    (user_id, nickname, now),
                )
        return cursor.rowcount > 0

    def recent(self, scope: str, limit: int = 24) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def message_count(self, scope: str) -> int:
        return int(
            self.db.execute(
                "SELECT count(*) FROM messages WHERE scope=? AND role='user'", (scope,)
            ).fetchone()[0]
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

    def remember(
        self,
        scope: str,
        user_id: str,
        content: str,
        *,
        importance: int = 1,
        source_event_id: str | None = None,
    ) -> int:
        content = content.strip()[:1000]
        if not content:
            raise ValueError("empty memory")
        cursor = self.db.execute(
            "INSERT INTO memories(scope,user_id,content,importance,source_event_id,created_at) VALUES(?,?,?,?,?,?)",
            (
                scope,
                user_id,
                content,
                max(1, min(5, importance)),
                source_event_id,
                int(time.time()),
            ),
        )
        return int(cursor.lastrowid)

    def search_memories(self, scope: str, query: str, limit: int = 5) -> list[dict]:
        words = [w for w in query.replace('"', " ").split() if w]
        if not words:
            return []
        fts_query = " OR ".join('"' + w[:40] + '"' for w in words[:8])
        try:
            rows = self.db.execute(
                """SELECT m.*, bm25(memories_fts) AS rank FROM memories_fts
                   JOIN memories m ON m.id=memories_fts.rowid
                   WHERE memories_fts MATCH ? AND m.scope=? ORDER BY rank, m.importance DESC LIMIT ?""",
                (fts_query, scope, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        if not rows:
            # FTS tokenization may not match unsegmented Chinese phrases.
            rows = self.db.execute(
                "SELECT * FROM memories WHERE scope=? AND content LIKE ? ORDER BY importance DESC,created_at DESC LIMIT ?",
                (scope, f"%{query[:80]}%", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_memory(self, scope: str, user_id: str, content: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM memories WHERE scope=? AND user_id=? AND content=? LIMIT 1",
                (scope, user_id, content),
            ).fetchone()
            is not None
        )

    def forget_user(self, user_id: str) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM messages WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM memories WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM relations WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM affection_events WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM people WHERE user_id=?", (user_id,))

    def proactive_count_since(
        self, group_id: str, since: int, source: str | None = None
    ) -> int:
        """Count posts since ``since``. ``source=None`` counts every pool."""
        sql = "SELECT count(*) FROM proactive_log WHERE group_id=? AND created_at>=?"
        params: list[Any] = [group_id, since]
        if source is not None:
            sql += " AND source=?"
            params.append(source)
        return int(self.db.execute(sql, params).fetchone()[0])

    def last_proactive(self, group_id: str) -> int:
        row = self.db.execute(
            "SELECT created_at FROM proactive_log WHERE group_id=? ORDER BY id DESC LIMIT 1",
            (group_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def log_proactive(
        self, group_id: str, content: str, source: str = "random"
    ) -> None:
        self.db.execute(
            "INSERT INTO proactive_log(group_id,content,created_at,source) VALUES(?,?,?,?)",
            (group_id, content, int(time.time()), source),
        )

    def replace_config_jobs(
        self, jobs: list[tuple[str, str, str, str, str, str, int]], now: int
    ) -> None:
        wanted = {entry[0] for entry in jobs}
        with self.transaction():
            for key, group, kind, value, prompt, action, next_run in jobs:
                existing = self.db.execute(
                    "SELECT * FROM jobs WHERE config_key=?", (key,)
                ).fetchone()
                if existing is None:
                    self.db.execute(
                        "INSERT INTO jobs(config_key,group_id,schedule_kind,schedule_value,prompt,action,next_run,created_by,created_at) VALUES(?,?,?,?,?,?,?,'config',?)",
                        (key, group, kind, value, prompt, action, next_run, now),
                    )
                elif any(
                    existing[field] != val
                    for field, val in (
                        ("group_id", group),
                        ("schedule_kind", kind),
                        ("schedule_value", value),
                        ("prompt", prompt),
                        ("action", action),
                    )
                ):
                    self.db.execute(
                        "UPDATE jobs SET group_id=?,schedule_kind=?,schedule_value=?,prompt=?,action=?,next_run=?,enabled=1 WHERE config_key=?",
                        (group, kind, value, prompt, action, next_run, key),
                    )
            for row in self.db.execute(
                "SELECT config_key FROM jobs WHERE created_by='config'"
            ):
                if row[0] not in wanted:
                    self.db.execute(
                        "UPDATE jobs SET enabled=0 WHERE config_key=?", (row[0],)
                    )

    def list_jobs(self, group_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM jobs WHERE group_id=? ORDER BY next_run", (group_id,)
            )
        ]

    def disable_job(self, group_id: str, job_id: int) -> bool:
        return (
            self.db.execute(
                "UPDATE jobs SET enabled=0 WHERE id=? AND group_id=?",
                (job_id, group_id),
            ).rowcount
            > 0
        )

    def due_jobs(self, now: int, limit: int = 10) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM jobs WHERE enabled=1 AND next_run<=? ORDER BY next_run LIMIT ?",
                (now, limit),
            )
        ]

    def reserve_job(self, job: dict, next_run: int | None, now: int) -> int | None:
        with self.transaction():
            updated = self.db.execute(
                "UPDATE jobs SET next_run=?,enabled=? WHERE id=? AND next_run=? AND enabled=1",
                (
                    next_run or now,
                    int(next_run is not None),
                    job["id"],
                    job["next_run"],
                ),
            )
            if not updated.rowcount:
                return None
            cursor = self.db.execute(
                "INSERT INTO job_runs(job_id,status,started_at) VALUES(?,?,?)",
                (job["id"], "running", now),
            )
            return int(cursor.lastrowid)

    def finish_job(self, run_id: int, status: str, detail: str, now: int) -> None:
        self.db.execute(
            "UPDATE job_runs SET status=?,detail=?,finished_at=? WHERE id=?",
            (status, detail[:500], now, run_id),
        )

    def reconcile_interrupted_jobs(self, now: int) -> None:
        self.db.execute(
            "UPDATE job_runs SET status='interrupted',detail='process restarted',finished_at=? WHERE status='running'",
            (now,),
        )

    def missed_jobs(self, before: int) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM jobs WHERE enabled=1 AND next_run<?", (before,)
            )
        ]

    def skip_job(self, job: dict, next_run: int | None, now: int) -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE jobs SET next_run=?,enabled=? WHERE id=?",
                (next_run or now, int(next_run is not None), job["id"]),
            )
            self.db.execute(
                "INSERT INTO job_runs(job_id,status,detail,started_at,finished_at) VALUES(?,?,?,?,?)",
                (job["id"], "skipped", "missed while offline", now, now),
            )
