from __future__ import annotations

import json
import logging

from ..scheduling.spec import RUN_ABANDONED
from .base import SqliteRepository

log = logging.getLogger(__name__)

#: Name of the single advisory row that decides which process runs the loop.
SCHEDULER_LEASE = "scheduler"


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY, group_id TEXT NOT NULL,
          schedule_kind TEXT NOT NULL, schedule_value TEXT NOT NULL,
          prompt TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          next_run INTEGER NOT NULL, created_by TEXT NOT NULL,
          created_at INTEGER NOT NULL, config_key TEXT UNIQUE,
          action TEXT NOT NULL DEFAULT 'chat',
          payload TEXT, spec_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS job_runs (
          id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
          status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          started_at INTEGER NOT NULL, finished_at INTEGER,
          lease_owner TEXT, lease_expires_at INTEGER,
          attempt INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS scheduler_leases (
          name TEXT PRIMARY KEY, owner TEXT NOT NULL,
          acquired_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
        );
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    if "config_key" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN config_key TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_config_key ON jobs(config_key)")
    if "action" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN action TEXT NOT NULL DEFAULT 'chat'")
    # Rows written before typed payloads read back as spec version 1 with an
    # empty payload, which is exactly how a prompt-only job behaves. Nothing
    # to backfill, and no run history is rewritten.
    if "payload" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN payload TEXT")
    if "spec_version" not in columns:
        db.execute("ALTER TABLE jobs ADD COLUMN spec_version INTEGER NOT NULL DEFAULT 1")
    run_columns = {row[1] for row in db.execute("PRAGMA table_info(job_runs)")}
    if "lease_owner" not in run_columns:
        db.execute("ALTER TABLE job_runs ADD COLUMN lease_owner TEXT")
    if "lease_expires_at" not in run_columns:
        db.execute("ALTER TABLE job_runs ADD COLUMN lease_expires_at INTEGER")
    if "attempt" not in run_columns:
        db.execute("ALTER TABLE job_runs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1")


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

        Each row is the historical 7-tuple, optionally followed by the typed
        payload dict. Keeping the payload as an eighth element means callers
        written before payloads existed keep working unchanged.
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
        from ..scheduling.spec import JOB_SPEC_VERSION, encode_payload

        for row in rows:
            key, group, kind, value, prompt, action, next_run = row[:7]
            payload = encode_payload(row[7] if len(row) > 7 else {})
            existing = self.db.execute(
                "SELECT created_by, group_id, schedule_kind, schedule_value, prompt, action, enabled, payload FROM jobs WHERE config_key=?",
                (key,),
            ).fetchone()
            if existing is None:
                self.db.execute(
                    "INSERT INTO jobs(config_key,group_id,schedule_kind,schedule_value,prompt,action,next_run,enabled,created_by,created_at,payload,spec_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        payload,
                        JOB_SPEC_VERSION,
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
            # Compared as JSON, not as text: a row written before payloads
            # existed is NULL and a config job with no payload is "{}". Those
            # mean the same thing, and treating them as an edit would reset
            # next_run (and resurrect a finished one-shot) on the upgrade sync.
            try:
                old_payload = json.loads(existing["payload"]) if existing["payload"] else {}
            except ValueError:
                old_payload = None
            edited = old_payload != json.loads(payload) or any(
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
                    "UPDATE jobs SET created_by=?,group_id=?,schedule_kind=?,schedule_value=?,prompt=?,action=?,next_run=?,enabled=?,payload=?,spec_version=? WHERE config_key=?",
                    (
                        created_by,
                        group,
                        kind,
                        value,
                        prompt,
                        action,
                        next_run,
                        enabled,
                        payload,
                        JOB_SPEC_VERSION,
                        key,
                    ),
                )

    def _hydrate(self, row) -> dict:
        """A jobs row as a dict, with ``payload`` parsed for handlers."""
        item = dict(row)
        raw = item.get("payload")
        if raw:
            try:
                item["payload"] = json.loads(raw)
            except ValueError as exc:
                raise ValueError(
                    f"job {item.get('config_key')!r} has a corrupt payload: {exc}"
                ) from exc
        else:
            item["payload"] = {}
        return item

    def list_jobs(self, group_id: str) -> list[dict]:
        return [
            self._hydrate(r)
            for r in self.db.execute(
                "SELECT * FROM jobs WHERE group_id=? ORDER BY next_run", (group_id,)
            )
        ]

    def all_jobs(self) -> list[dict]:
        """Every job row, for a startup check that must not miss a group."""
        return [self._hydrate(r) for r in self.db.execute("SELECT * FROM jobs")]

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
            self._hydrate(r)
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
        """Settle a run. Only a ``running`` row can move.

        The guard is what makes the idempotency key real: a run that was already
        recovered as ``interrupted`` by a restart cannot later be rewritten by a
        late writer, so one run id has exactly one recorded outcome.
        """
        self.db.execute(
            "UPDATE job_runs SET status=?,detail=?,finished_at=? WHERE id=? AND status='running'",
            (status, detail[:500], now, run_id),
        )

    def job_run(self, run_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM job_runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def reconcile_interrupted_jobs(self, now: int) -> None:
        """Single-instance restart: nothing can still be live, so none of these
        runs will ever report back."""
        self.db.execute(
            "UPDATE job_runs SET status='interrupted',detail='process restarted',finished_at=? WHERE status='running'",
            (now,),
        )

    def reconcile_owned_runs(self, owner: str, now: int) -> None:
        """Multi-instance restart: only the runs this process was holding."""
        self.db.execute(
            "UPDATE job_runs SET status='interrupted',detail='process restarted',finished_at=? WHERE status='running' AND lease_owner=?",
            (now, owner),
        )

    def claim_run_lease(self, run_id: int, owner: str, now: int, ttl: int) -> bool:
        """Take or renew the lease on one running run."""
        return (
            self.db.execute(
                "UPDATE job_runs SET lease_owner=?,lease_expires_at=? "
                "WHERE id=? AND status='running' "
                "AND (lease_owner IS NULL OR lease_owner=? OR lease_expires_at<=?)",
                (owner, now + ttl, run_id, owner, now),
            ).rowcount
            > 0
        )

    def expire_run_leases(self, now: int) -> int:
        """Free runs whose holder died. Bounded by the lease, not by a restart."""
        return self.db.execute(
            "UPDATE job_runs SET status=?,detail='lease expired',finished_at=? "
            "WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
            (RUN_ABANDONED, now, now),
        ).rowcount

    def claim_scheduler_lease(self, owner: str, now: int, ttl: int) -> bool:
        """Become the one process allowed to run the scheduled loop.

        Atomic on purpose: two processes racing at startup both issue this
        statement and SQLite serialises them, so exactly one wins. A holder
        that dies stops renewing and the lease expires, which is why this must
        be renewed far more often than ``ttl``.
        """
        with self.transaction():
            return (
                self.db.execute(
                    "INSERT INTO scheduler_leases(name,owner,acquired_at,expires_at) "
                    "VALUES(?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, "
                    "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at "
                    "WHERE scheduler_leases.owner=excluded.owner "
                    "OR scheduler_leases.expires_at<=?",
                    (SCHEDULER_LEASE, owner, now, now + ttl, now),
                ).rowcount
                > 0
            )

    def release_scheduler_lease(self, owner: str) -> None:
        self.db.execute(
            "DELETE FROM scheduler_leases WHERE name=? AND owner=?",
            (SCHEDULER_LEASE, owner),
        )

    def scheduler_lease_owner(self, now: int) -> str | None:
        row = self.db.execute(
            "SELECT owner FROM scheduler_leases WHERE name=? AND expires_at>?",
            (SCHEDULER_LEASE, now),
        ).fetchone()
        return row[0] if row else None

    def missed_jobs(self, before: int) -> list[dict]:
        return [
            self._hydrate(r)
            for r in self.db.execute(
                "SELECT * FROM jobs WHERE enabled=1 AND next_run<?", (before,)
            )
        ]

    def skip_job(
        self, job: dict, next_run: int | None, now: int, detail: str = "missed while offline"
    ) -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE jobs SET next_run=?,enabled=? WHERE id=?",
                (next_run or now, int(next_run is not None), job["id"]),
            )
            self.db.execute(
                "INSERT INTO job_runs(job_id,status,detail,started_at,finished_at) VALUES(?,?,?,?,?)",
                (job["id"], "skipped", detail[:500], now, now),
            )
