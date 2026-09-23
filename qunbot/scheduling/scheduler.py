"""Persistent at/every/cron scheduling.

Two sources feed the jobs table:

* ``config/schedules.json`` — operator-defined, always enabled.
* handler suggestions — shipped by a task handler, seeded **disabled** so that
  dropping a handler file never starts posting on its own. Writing the same job
  into schedules.json claims the key and turns it on.

A due run is reserved before execution. Missed runs are not replayed after a
restart; that avoids surprise QQ spam.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from croniter import croniter

from ..domain import JobSkipped
from ..ports import JobRepository
from .registry import DEFAULT_ACTION, JobSuggestion

log = logging.getLogger(__name__)

Row = tuple[str, str, str, str, str, str, int]


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
    def __init__(
        self,
        jobs: JobRepository,
        tz_name: str,
        known_actions: frozenset[str] | None = None,
    ):
        self.jobs, self.tz_name = jobs, tz_name
        # Bootstrap passes the explicitly enabled actions. No file scanning.
        self.known_actions = (
            known_actions if known_actions is not None else frozenset({DEFAULT_ACTION})
        )
        ZoneInfo(tz_name)

    def _validate_schedule(
        self, kind: str, value: str, prompt: str, action: str, key: str = ""
    ) -> None:
        """Checks that apply to a config job and a handler suggestion alike."""
        if action not in self.known_actions:
            # Name the job. "must be one of []" with no context is what a
            # deployer sees after trimming BOT_EXTENSIONS while a job in
            # config/schedules.json still needs that extension — which is
            # exactly the moment they need to be told which job to look at.
            where = f"job {key!r} " if key else "job "
            raise ValueError(
                f"{where}wants action {action!r}, which no enabled extension "
                f"provides (enabled actions: {sorted(self.known_actions) or 'none'}). "
                "Add the extension that owns it to BOT_EXTENSIONS, or remove "
                "the job from the schedule."
            )
        # Conversational jobs need a prompt; other actions decide for themselves.
        minimum = 0 if action != DEFAULT_ACTION else 1
        if not minimum <= len(prompt) <= 500:
            raise ValueError(
                f"job prompt must be {minimum}-500 characters for {action}"
            )
        if kind == "at":
            datetime.fromisoformat(value)

    def _next_run(self, kind: str, value: str, now: int, *, past_is_ok: bool) -> int:
        try:
            return next_occurrence(kind, value, after=now, tz_name=self.tz_name)
        except ValueError:
            if kind != "at" or not past_is_ok:
                raise
            # Preserve already completed one-shot jobs on restart. A newly
            # configured past job is recorded as skipped, never replayed.
            return now - 31

    def sync_config(
        self,
        path: Path,
        allowed_groups: frozenset[str],
        suggestions: Iterable[tuple[str, JobSuggestion]] = (),
    ) -> None:
        """Apply local config, then seed handler suggestions as disabled rows."""
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("jobs", [])
        if not isinstance(entries, list):
            raise TypeError("schedules.jobs must be a list")
        now = int(time.time())
        seen_keys: set[str] = set()
        configured: list[Row] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise TypeError("each job must be an object")
            key = str(entry.get("id", "")).strip()
            if not key:
                raise ValueError("each schedule requires a nonempty id")
            if key in seen_keys:
                raise ValueError(f"duplicate schedule id: {key}")
            seen_keys.add(key)
            group_id = str(entry.get("group_id", ""))
            if group_id not in allowed_groups:
                raise ValueError(f"scheduled group {group_id} is not allowlisted")
            kind = str(entry.get("kind", ""))
            value = str(entry.get("value", ""))
            action = str(entry.get("action", DEFAULT_ACTION)).strip() or DEFAULT_ACTION
            prompt = str(entry.get("prompt", "")).strip()
            self._validate_schedule(kind, value, prompt, action, key)
            configured.append(
                (
                    key,
                    group_id,
                    kind,
                    value,
                    prompt,
                    action,
                    self._next_run(kind, value, now, past_is_ok=True),
                )
            )

        # A suggestion that duplicates a schedule the operator already runs is
        # noise, so it is not seeded at all. Keyed on group+action+value rather
        # than id, so it also covers jobs written under a different id.
        running = {(row[1], row[5], row[3]) for row in configured}
        self.jobs.sync_jobs(
            configured,
            [
                row
                for row in self._expand(suggestions, allowed_groups, now)
                if (row[1], row[5], row[3]) not in running
            ],
            now,
        )

    def _expand(
        self,
        suggestions: Iterable[tuple[str, JobSuggestion]],
        allowed_groups: frozenset[str],
        now: int,
    ) -> list[Row]:
        """Fan a per-handler suggestion out across every allowlisted group."""
        rows: list[Row] = []
        for action, item in suggestions:
            self._validate_schedule(item.kind, item.value, item.prompt, action, item.id)
            next_run = self._next_run(item.kind, item.value, now, past_is_ok=False)
            for group_id in sorted(allowed_groups):
                rows.append(
                    (
                        f"{item.id}@{group_id}",
                        group_id,
                        item.kind,
                        item.value,
                        item.prompt,
                        action,
                        next_run,
                    )
                )
        return rows

    def list(self, group_id: str) -> list[dict]:
        return self.jobs.list_jobs(group_id)

    def disable(self, group_id: str, job_id: int) -> bool:
        return self.jobs.disable_job(group_id, job_id)

    def reserve_due(self, now: int | None = None) -> list[dict]:
        now = now or int(time.time())
        reserved = []
        for row in self.jobs.due_jobs(now):
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
