from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from .domain import JobSkipped
from .ports import JobRepository

log = logging.getLogger(__name__)

JOB_ACTIONS = frozenset({"chat", "poster"})


def next_occurrence(kind: str, value: str, *, after: int, tz_name: str) -> int:
    if kind == "at":
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=ZoneInfo(tz_name))
        timestamp = int(result.timestamp())
        if timestamp <= after:
            raise ValueError("one-shot time must be in the future")
        return timestamp
    if kind == "every":
        seconds = int(value)
        if seconds < 300:
            raise ValueError("minimum interval is 300 seconds")
        return after + seconds
    if kind == "cron":
        if len(value.split()) != 5:
            raise ValueError("cron expression must have five fields")
        base = datetime.fromtimestamp(after, ZoneInfo(tz_name))
        return int(croniter(value, base).get_next(datetime).timestamp())
    raise ValueError(f"unsupported schedule: {kind}")


class Scheduler:
    """Persistent at/every/cron jobs. A due run is reserved before execution.

    Missed runs are not replayed after restart; this avoids surprise QQ spam.
    """

    def __init__(self, jobs: JobRepository, tz_name: str):
        self.jobs, self.tz_name = jobs, tz_name
        ZoneInfo(tz_name)

    def sync_config(self, path: Path, allowed_groups: frozenset[str]) -> None:
        """Only local config can define jobs. Restart applies changes atomically."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("jobs", [])
        if not isinstance(entries, list):
            raise TypeError("schedules.jobs must be a list")
        now = int(time.time())
        checked: list[tuple[str, str, str, str, str, str, int]] = []
        seen_keys: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError("each job must be an object")
            key = str(entry.get("id", "")).strip()
            if not key or key in seen_keys:
                raise ValueError("each schedule requires a unique nonempty id")
            seen_keys.add(key)
            group_id = str(entry.get("group_id", ""))
            if group_id not in allowed_groups:
                raise ValueError(f"scheduled group {group_id} is not allowlisted")
            kind = str(entry.get("kind", ""))
            value = str(entry.get("value", ""))
            action = str(entry.get("action", "chat")).strip() or "chat"
            if action not in JOB_ACTIONS:
                raise ValueError(f"job action must be one of {sorted(JOB_ACTIONS)}")
            prompt = str(entry.get("prompt", "")).strip()
            # A poster caption is optional; the poster itself is always drawn.
            minimum = 0 if action == "poster" else 1
            if not minimum <= len(prompt) <= 500:
                raise ValueError(
                    f"job prompt must be {minimum}-500 characters for {action}"
                )
            if kind == "at":
                datetime.fromisoformat(value)
            try:
                next_run = next_occurrence(kind, value, after=now, tz_name=self.tz_name)
            except ValueError:
                if kind != "at":
                    raise
                # Preserve already completed one-shot jobs on restart. A newly
                # configured past job is recorded as skipped, never replayed.
                next_run = now - 31
            checked.append((key, group_id, kind, value, prompt, action, next_run))
        self.jobs.replace_config_jobs(checked, now)

    def list(self, group_id: str) -> list[dict]:
        return self.jobs.list_jobs(group_id)

    def disable(self, group_id: str, job_id: int) -> bool:
        return self.jobs.disable_job(group_id, job_id)

    def reserve_due(self, now: int | None = None) -> list[dict]:
        now = now or int(time.time())
        rows = self.jobs.due_jobs(now)
        reserved = []
        for row in rows:
            job = dict(row)
            try:
                upcoming = (
                    next_occurrence(
                        job["schedule_kind"],
                        job["schedule_value"],
                        after=now,
                        tz_name=self.tz_name,
                    )
                    if job["schedule_kind"] != "at"
                    else None
                )
            except (ValueError, OverflowError):
                upcoming = None
            run_id = self.jobs.reserve_job(job, upcoming, now)
            if run_id is not None:
                job["run_id"] = run_id
                reserved.append(job)
        return reserved

    def finish(self, run_id: int, status: str, detail: str = "") -> None:
        self.jobs.finish_job(run_id, status, detail, int(time.time()))

    def reconcile_interrupted(self) -> None:
        self.jobs.reconcile_interrupted_jobs(int(time.time()))

    def skip_missed(self) -> None:
        now = int(time.time())
        for row in self.jobs.missed_jobs(now - 30):
            try:
                upcoming = (
                    next_occurrence(
                        row["schedule_kind"],
                        row["schedule_value"],
                        after=now,
                        tz_name=self.tz_name,
                    )
                    if row["schedule_kind"] != "at"
                    else None
                )
            except (ValueError, OverflowError):
                upcoming = None
            self.jobs.skip_job(row, upcoming, now)

    async def _execute(self, callback, job: dict) -> None:
        try:
            await callback(job)
        except JobSkipped as exc:
            log.info("Scheduled job %s skipped: %s", job["id"], exc)
            self.finish(job["run_id"], "skipped", str(exc))
        except Exception as exc:
            log.exception("Scheduled job %s failed", job["id"])
            self.finish(job["run_id"], "failed", str(exc))
        else:
            self.finish(job["run_id"], "succeeded")

    async def loop(self, callback) -> None:
        self.reconcile_interrupted()
        self.skip_missed()
        while True:
            # One slow model call must not hold up the other due jobs. Jobs for
            # the same group still serialise on the service-side scope lock.
            await asyncio.gather(
                *(self._execute(callback, job) for job in self.reserve_due())
            )
            await asyncio.sleep(15)
