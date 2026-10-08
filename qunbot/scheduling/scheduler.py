"""Persistent at/every/cron scheduling.

Two sources feed the jobs table:

* ``config/schedules.json`` — operator-defined, always enabled.
* handler suggestions — shipped by a task handler, seeded **disabled** so that
  dropping a handler file never starts posting on its own. Writing the same job
  into schedules.json claims the key and turns it on.

A due run is reserved before execution: advancing ``next_run`` and inserting
the ``running`` row happen in one transaction, so a given ``run_id`` can be
handed out exactly once no matter how the process dies afterwards.

Explicit policies, because "whatever the code happens to do" is not a policy:

* **missed one-shot** (``at``) — never replayed. Recorded ``skipped``.
* **missed periodic** (``every``/``cron``) — *coalesced, never replayed*. At
  most one run is ever attributed to a gap: within :data:`MISFIRE_GRACE` the
  job runs once, late; beyond it the gap is recorded ``skipped`` and the job
  resumes on its normal cadence. Ten queued lunch-time posts at 6pm would be
  spam, and a 7am greeting posted at 3pm is worse than none.
* **timeout** — a handler that outlives ``job_timeout`` is a failure, not a
  quiet skip, and its run row is settled instead of sitting ``running``.
* **cron is not "the bot must talk"** — a handler that decides this is not the
  moment raises :class:`~qunbot.domain.JobSkipped` and the run is ``skipped``.

Single instance is still the deployment assumption. ``instance_id`` is opt-in:
leave it ``None`` (what ``app.py`` does) and the loop behaves exactly as before.
Pass one and the loop takes a renewable lease, so a second process idles
instead of double-posting. Retries for a failed run are deliberately *not*
implemented; the lease has to be proven first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from ..domain import JobSkipped
from ..ports import JobRepository
from .registry import DEFAULT_ACTION, JobSuggestion
from .spec import (
    JOB_SPEC_VERSION,
    RUN_FAILED,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    JobSpec,
    JobTimeout,
    check_payload,
)

log = logging.getLogger(__name__)

#: A jobs row plus its typed payload. The first seven elements are the
#: historical shape; callers that predate payloads may still pass just those.
Row = tuple

#: A run is "missed" once it is this far past due, so clock jitter and the
#: 15s loop tick do not manufacture misfires.
MISSED_MARGIN_SECONDS = 30

#: Default catch-up window for a periodic job that missed its slot. Matching
#: the minimum ``every`` interval keeps "late" and "next" in the same units.
MISFIRE_GRACE_SECONDS = 300

#: A stuck handler is cancelled and settled after this long.
DEFAULT_JOB_TIMEOUT = 300.0

#: How long a cancelled handler is given to unwind before we stop waiting.
#: Bounded on purpose: the run row is already settled, and a handler that
#: swallows CancelledError must not be able to block the loop forever.
CANCEL_REAP_SECONDS = 5.0

#: Lease lifetime and renewal cadence. The lease is renewed every loop tick,
#: which is far shorter than the ttl, so a live process never loses it.
DEFAULT_LEASE_TTL = 120
LOOP_INTERVAL_SECONDS = 15


def next_occurrence(kind: str, value: str, *, after: int, tz_name: str) -> int:
    """The first occurrence strictly after ``after`` (a unix timestamp).

    ``at`` values without a zone are read in ``tz_name``; an explicit offset in
    the value always wins. Wall-clock schedules are evaluated in ``tz_name``,
    which is what makes a 07:00 cron mean 07:00 local on both sides of a DST
    switch (see ``count_missed`` for the interval bookkeeping that implies).
    """
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


def count_missed(
    kind: str, value: str, *, since: int, now: int, tz_name: str, limit: int = 100
) -> int:
    """Occurrences in ``(since, now]`` — how big the gap a skip is covering is.

    Only used for the run detail, so ``limit`` bounds the work rather than the
    truth: a job that missed more than ``limit`` slots reports the cap.
    """
    if now <= since:
        return 0
    if kind == "at":
        return 1
    if kind == "every":
        try:
            seconds = int(value)
        except ValueError:
            return 1
        return max(1, min(limit, (now - since) // seconds))
    if kind != "cron":
        return 1
    try:
        if len(value.split()) != 5:
            return 1
        iterator = croniter(value, datetime.fromtimestamp(since, ZoneInfo(tz_name)))
        count = 0
        while count < limit:
            if int(iterator.get_next(datetime).timestamp()) > now:
                break
            count += 1
        return max(1, count)
    except (ValueError, OverflowError):
        return 1


class Scheduler:
    def __init__(
        self,
        jobs: JobRepository,
        tz_name: str,
        known_actions: frozenset[str] | None = None,
        *,
        payload_validators: Mapping[str, Callable[[Mapping[str, Any]], None]] | None = None,
        job_timeout: float = DEFAULT_JOB_TIMEOUT,
        misfire_grace: int = MISFIRE_GRACE_SECONDS,
        instance_id: str | None = None,
        lease_ttl: int = DEFAULT_LEASE_TTL,
    ):
        self.jobs, self.tz_name = jobs, tz_name
        # Bootstrap passes the explicitly enabled actions. No file scanning.
        self.known_actions = (
            known_actions if known_actions is not None else frozenset({DEFAULT_ACTION})
        )
        # Per-action payload checks, supplied at registration. The scheduler
        # validates the envelope; only the action knows what its keys mean.
        self.payload_validators = dict(payload_validators or {})
        self.job_timeout = job_timeout
        self.misfire_grace = misfire_grace
        #: ``None`` means "assume I am the only process" — today's deployment.
        self.instance_id = instance_id
        self.lease_ttl = lease_ttl
        ZoneInfo(tz_name)

    def _validate_schedule(
        self,
        kind: str,
        value: str,
        prompt: str,
        action: str,
        key: str = "",
        payload: Mapping[str, Any] | None = None,
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
        self._validate_payload(action, payload, key)

    def _validate_payload(
        self, action: str, payload: Mapping[str, Any] | None, key: str
    ) -> None:
        """Envelope first, then whatever the action says it needs."""
        check_payload(payload, key=key)
        validator = self.payload_validators.get(action)
        if validator is None:
            return
        try:
            validator(payload or {})
        except Exception as exc:
            raise ValueError(
                f"job {key!r} has an invalid payload for action {action!r}: {exc}"
            ) from exc

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
            payload = entry.get("payload")
            # ``None`` preserves the historical behavior for old files: a
            # completed one-shot without an explicit flag stays disabled on
            # restart. WebUI-managed entries always write the flag.
            enabled = bool(entry["enabled"]) if "enabled" in entry else None
            self._validate_schedule(kind, value, prompt, action, key, payload)
            configured.append(
                (
                    key,
                    group_id,
                    kind,
                    value,
                    prompt,
                    action,
                    self._next_run(kind, value, now, past_is_ok=True),
                    check_payload(payload, key=key),
                    enabled,
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
        self.check_specs()

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
                        {},
                    )
                )
        return rows

    def check_specs(self) -> None:
        """Startup check over every stored job.

        Catches what config validation cannot see: a row that is *enabled* in
        the database but whose action no longer has a handler (the extension
        was removed after the job was turned on), and a row written by a newer
        build whose spec this one cannot read. Both are raised here, at
        startup, rather than discovered mid-post — the group must never see an
        exception, and the operator must be told which job to look at.

        Disabled rows are allowed to linger with an unknown action: that is
        what a stale handler suggestion looks like after trimming
        BOT_EXTENSIONS, and it is harmless until someone enables it.
        """
        for row in self.jobs.all_jobs():
            spec = JobSpec.from_row(row)  # rejects an unreadable spec version
            spec.validate()
            if not row.get("enabled"):
                continue
            if spec.action not in self.known_actions:
                where = f"job {spec.key!r} " if spec.key else "job "
                raise ValueError(
                    f"{where}is enabled and wants action {spec.action!r}, which no "
                    f"enabled extension provides (enabled actions: "
                    f"{sorted(self.known_actions) or 'none'}). Add the extension "
                    "that owns it to BOT_EXTENSIONS, or remove the job from "
                    "the schedule."
                )
            self._validate_payload(spec.action, spec.payload, spec.key)

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
                if self.instance_id:
                    self.jobs.claim_run_lease(
                        run_id, self.instance_id, now, self.lease_ttl
                    )
                reserved.append(job)
        return reserved

    def finish(self, run_id: int, status: str, detail: str = "") -> None:
        self.jobs.finish_job(run_id, status, detail, int(time.time()))

    def reconcile_interrupted(self, now: int | None = None) -> None:
        """Settle runs left behind by a process that is gone.

        Single instance: every ``running`` row belongs to a dead process, so
        all of them are recovered. With an instance id we own only our own
        rows, plus any run whose lease has expired.
        """
        now = now if now is not None else int(time.time())
        if self.instance_id is None:
            self.jobs.reconcile_interrupted_jobs(now)
            return
        self.jobs.expire_run_leases(now)
        self.jobs.reconcile_owned_runs(self.instance_id, now)

    def skip_missed(self, now: int | None = None) -> list[dict]:
        """Apply the misfire policy to everything past due at startup.

        Returns the jobs that were *deferred* to a single late run rather than
        skipped. They are left due on purpose: the caller's normal
        ``reserve_due`` pass then executes each of them exactly once, which is
        the whole point of coalescing instead of replaying.
        """
        now = now if now is not None else int(time.time())
        deferred: list[dict] = []
        for row in self.jobs.missed_jobs(now - MISSED_MARGIN_SECONDS):
            if row["schedule_kind"] != "at" and now - row["next_run"] <= self.misfire_grace:
                deferred.append(dict(row))
                continue
            missed = count_missed(
                row["schedule_kind"],
                row["schedule_value"],
                since=row["next_run"],
                now=now,
                tz_name=self.tz_name,
            )
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
            detail = (
                "missed while offline"
                if row["schedule_kind"] == "at"
                else f"missed {missed} occurrence(s) while offline; not replayed"
            )
            self.jobs.skip_job(row, upcoming, now, detail)
        return deferred

    async def _run_with_timeout(self, callback, job: dict) -> None:
        """Run the handler, cancelling it if it outlives ``job_timeout``."""
        task = asyncio.ensure_future(callback(job))
        done, _ = await asyncio.wait({task}, timeout=self.job_timeout)
        if done:
            # ``result`` re-raises whatever the handler raised, with traceback.
            task.result()
            return
        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), CANCEL_REAP_SECONDS
            )
        except (TimeoutError, asyncio.CancelledError):
            # A handler that swallows CancelledError is abandoned, not waited
            # on: its run row is about to be settled either way.
            log.error("Handler for job %s ignored cancellation", job["id"])
        raise JobTimeout(
            f"timed out after {self.job_timeout:g}s; handler cancelled"
        )

    async def _execute(self, callback, job: dict) -> None:
        try:
            await self._run_with_timeout(callback, job)
        except JobTimeout as exc:
            log.error("Scheduled job %s timed out: %s", job["id"], exc)
            self.finish(job["run_id"], RUN_FAILED, str(exc))
        except JobSkipped as exc:
            log.info("Scheduled job %s skipped: %s", job["id"], exc)
            self.finish(job["run_id"], RUN_SKIPPED, str(exc))
        except Exception as exc:
            log.exception("Scheduled job %s failed", job["id"])
            self.finish(job["run_id"], RUN_FAILED, str(exc))
        else:
            self.finish(job["run_id"], RUN_SUCCEEDED)

    def _hold_lease(self, now: int | None = None) -> bool:
        """True if this process may run the loop right now."""
        if self.instance_id is None:
            return True
        now = now if now is not None else int(time.time())
        return self.jobs.claim_scheduler_lease(
            self.instance_id, now, self.lease_ttl
        )

    def _release_lease(self) -> None:
        if self.instance_id is None:
            return
        try:
            self.jobs.release_scheduler_lease(self.instance_id)
        except Exception:  # pragma: no cover - shutdown must not raise
            log.exception("Could not release the scheduler lease")

    async def loop(self, callback) -> None:
        self.reconcile_interrupted()
        self.skip_missed()
        try:
            while True:
                if not self._hold_lease():
                    # Another process owns the schedule. Idle quietly; it will
                    # post, we must not.
                    await asyncio.sleep(LOOP_INTERVAL_SECONDS)
                    continue
                # One slow model call must not hold up the other due jobs. Jobs
                # for the same group still serialise on the service-side scope
                # lock.
                await asyncio.gather(
                    *(self._execute(callback, job) for job in self.reserve_due())
                )
                await asyncio.sleep(LOOP_INTERVAL_SECONDS)
        finally:
            self._release_lease()


__all__ = [
    "DEFAULT_JOB_TIMEOUT",
    "DEFAULT_LEASE_TTL",
    "MISFIRE_GRACE_SECONDS",
    "MISSED_MARGIN_SECONDS",
    "Scheduler",
    "count_missed",
    "next_occurrence",
]
