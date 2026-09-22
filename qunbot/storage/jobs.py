from __future__ import annotations

from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY, group_id TEXT NOT NULL,
          schedule_kind TEXT NOT NULL, schedule_value TEXT NOT NULL,
          prompt TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          next_run INTEGER NOT NULL, created_by TEXT NOT NULL,
          created_at INTEGER NOT NULL, config_key TEXT UNIQUE,
          action TEXT NOT NULL DEFAULT 'chat'
        );
        CREATE TABLE IF NOT EXISTS job_runs (
          id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
          status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          started_at INTEGER NOT NULL, finished_at INTEGER
        );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    if "config_key" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN config_key TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_config_key ON jobs(config_key)")
    if "action" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN action TEXT NOT NULL DEFAULT 'chat'")


class JobsStore(SqliteRepository):

    def sync_jobs(
        self,
        configured: list[tuple[str, str, str, str, str, str, int]],
        suggested: list[tuple[str, str, str, str, str, str, int]],
        now: int,
    ) -> None:
        """Reconcile the jobs table against both sources.

        ``configured`` comes from config/schedules.json and always wins — it is
        enabled even if it used to be a disabled handler suggestion, and a
        change to its schedule resets the next run. ``suggested`` comes from
        handlers, is seeded disabled, and never touches an existing row, so an
        operator's edits survive. Keys that vanish from either source are
        disabled rather than deleted, keeping their run history.
        """
        with self.transaction():
            self._upsert_jobs(configured, now, created_by="config", enabled=1)
            self._upsert_jobs(suggested, now, created_by="handler", enabled=0)
            claimed = {entry[0] for entry in configured}
            for source, wanted in (
                ("config", claimed),
                ("handler", {entry[0] for entry in suggested} - claimed),
            ):
                for row in self.db.execute(
                    "SELECT config_key FROM jobs WHERE created_by=?", (source,)
                ):
                    if row[0] not in wanted:
                        self.db.execute(
                            "UPDATE jobs SET enabled=0 WHERE config_key=?", (row[0],)
                        )

    def _upsert_jobs(
        self, rows: list[tuple], now: int, *, created_by: str, enabled: int
    ) -> None:
        for key, group, kind, value, prompt, action, next_run in rows:
            existing = self.db.execute(
                "SELECT created_by, group_id, schedule_kind, schedule_value, prompt, action, enabled FROM jobs WHERE config_key=?",
                (key,),
            ).fetchone()
            if existing is None:
                self.db.execute(
                    "INSERT INTO jobs(config_key,group_id,schedule_kind,schedule_value,prompt,action,next_run,enabled,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        group,
                        kind,
                        value,
                        prompt,
                        action,
                        next_run,
                        enabled,
                        created_by,
                        now,
                    ),
                )
                continue
            if created_by == "handler":
                # A suggestion never disturbs an existing row: the operator may
                # have enabled it, and its run history must stay coherent.
                continue
            # Adoption covers a handler suggestion promoted to a config job;
            # the rest is a real edit. An untouched config job is left alone so
            # that a finished one-shot stays disabled instead of firing again.
            adopted = existing["created_by"] != created_by
            edited = any(
                existing[field] != val
                for field, val in (
                    ("group_id", group),
                    ("schedule_kind", kind),
                    ("schedule_value", value),
                    ("prompt", prompt),
                    ("action", action),
                )
            )
            if adopted or edited:
                # next_run is recomputed on adoption so a promoted suggestion
                # does not fire late.
                self.db.execute(
                    "UPDATE jobs SET created_by=?,group_id=?,schedule_kind=?,schedule_value=?,prompt=?,action=?,next_run=?,enabled=? WHERE config_key=?",
                    (
                        created_by,
                        group,
                        kind,
                        value,
                        prompt,
                        action,
                        next_run,
                        enabled,
                        key,
                    ),
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
