"""Scheduling infrastructure: time zones, misfires, timeouts, leases, payloads.

These tests exercise the engine only. No test here connects to NapCat, sends a
QQ message or reads the deployment's .env.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from qunbot.domain import JobSkipped
from qunbot.scheduling import (
    JOB_SPEC_VERSION,
    JobHandlerRegistry,
    JobSpec,
    Scheduler,
    count_missed,
    next_occurrence,
)
from qunbot.scheduling.runner import JobRunner
from support import Store


def ts(iso: str, tz: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(tz)).timestamp())


def local(epoch: int, tz: str) -> datetime:
    return datetime.fromtimestamp(epoch, ZoneInfo(tz))


class SchedulingTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def schedules(self, *jobs) -> Path:
        path = self.root / "schedules.json"
        path.write_text(json.dumps({"jobs": list(jobs)}), encoding="utf-8")
        return path

    def seed(self, next_run: int, *, kind="every", value="300", action="chat", prompt="p"):
        """Insert one config-owned job directly, bypassing the clock."""
        self.store.sync_jobs(
            [("j", "42", kind, value, prompt, action, next_run)], [], 0
        )
        return self.store.list_jobs("42")[0]

    def job_row(self, run_id: int) -> dict:
        row = self.store.job_run(run_id)
        self.assertIsNotNone(row)
        return row

    def run_count(self) -> int:
        return self.store.db.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0]

    def last_run(self) -> dict:
        row = self.store.db.execute(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row, "no run was recorded")
        return dict(row)


class DstTests(SchedulingTestCase):
    """A wall-clock schedule must mean local wall-clock, everywhere."""

    TZ = "America/New_York"

    def occurrences(self, expr: str, start_iso: str, end_iso: str) -> list[int]:
        cursor = ts(start_iso, self.TZ)
        end = ts(end_iso, self.TZ)
        found = []
        while True:
            nxt = next_occurrence("cron", expr, after=cursor, tz_name=self.TZ)
            if nxt > end:
                return found
            found.append(nxt)
            cursor = nxt

    def test_daily_morning_cron_fires_once_per_local_day_across_spring_forward(self):
        """The 07:00 slot is outside the 02:00 gap, so DST must not skip or
        duplicate it — the *interval* changes, not the number of posts."""
        found = self.occurrences("0 7 * * *", "2026-03-02T00:00:00", "2026-03-10T00:00:00")
        self.assertEqual(
            [local(x, self.TZ).strftime("%Y-%m-%d %H:%M") for x in found],
            [
                "2026-03-02 07:00",
                "2026-03-03 07:00",
                "2026-03-04 07:00",
                "2026-03-05 07:00",
                "2026-03-06 07:00",
                "2026-03-07 07:00",
                "2026-03-08 07:00",  # the switch day itself
                "2026-03-09 07:00",
            ],
        )
        # Exactly one run per local date: nothing dropped, nothing twice.
        dates = [local(x, self.TZ).date() for x in found]
        self.assertEqual(len(dates), len(set(dates)))
        offsets = {(found[i + 1] - found[i]) for i in range(len(found) - 1)}
        self.assertEqual(offsets, {23 * 3600, 24 * 3600})
        # The short day is the 8th: 07:00 EST to 07:00 EDT is 23 hours.
        self.assertEqual(
            ts("2026-03-08T07:00:00", self.TZ) - ts("2026-03-07T07:00:00", self.TZ),
            23 * 3600,
        )

    def test_daily_morning_cron_fires_once_per_local_day_across_fall_back(self):
        found = self.occurrences("0 7 * * *", "2026-10-30T00:00:00", "2026-11-04T00:00:00")
        self.assertEqual(
            [local(x, self.TZ).strftime("%Y-%m-%d %H:%M") for x in found],
            [
                "2026-10-30 07:00",
                "2026-10-31 07:00",
                "2026-11-01 07:00",  # the switch day itself
                "2026-11-02 07:00",
                "2026-11-03 07:00",
            ],
        )
        dates = [local(x, self.TZ).date() for x in found]
        self.assertEqual(len(dates), len(set(dates)))
        self.assertEqual(
            ts("2026-11-01T07:00:00", self.TZ) - ts("2026-10-31T07:00:00", self.TZ),
            25 * 3600,
        )

    def test_cron_inside_the_spring_forward_gap_still_fires_exactly_once(self):
        """02:30 does not exist on 2026-03-08. One run, not zero and not two:
        croniter clamps the slot forward, and the walk stays increasing."""
        found = self.occurrences("30 2 * * *", "2026-03-07T00:00:00", "2026-03-10T00:00:00")
        self.assertEqual(len(found), 3)
        self.assertEqual(
            [local(x, self.TZ).strftime("%Y-%m-%d %H:%M") for x in found],
            ["2026-03-07 02:30", "2026-03-08 03:00", "2026-03-09 02:30"],
        )
        self.assertEqual(found, sorted(set(found)))

    def test_an_ambiguous_local_hour_has_two_distinct_moments(self):
        """01:30 happens twice on 2026-11-01. Each occurrence is a distinct
        epoch, so the two runs have distinct run ids — the idempotency key
        holds even here. See the module report for the duplicated-post caveat."""
        found = self.occurrences("30 1 * * *", "2026-10-31T00:00:00", "2026-11-02T00:00:00")
        ambiguous = [x for x in found if local(x, self.TZ).date().isoformat() == "2026-11-01"]
        self.assertEqual(len(ambiguous), 2)
        self.assertNotEqual(*ambiguous)
        self.assertEqual(found, sorted(set(found)))

    def test_the_same_cron_is_a_different_moment_in_a_different_zone(self):
        shanghai = next_occurrence(
            "cron", "0 7 * * *", after=ts("2026-06-01T00:00:00", "Asia/Shanghai"),
            tz_name="Asia/Shanghai",
        )
        new_york = next_occurrence(
            "cron", "0 7 * * *", after=ts("2026-06-01T00:00:00", self.TZ), tz_name=self.TZ
        )
        self.assertNotEqual(shanghai, new_york)
        self.assertEqual(local(shanghai, "Asia/Shanghai").hour, 7)
        self.assertEqual(local(new_york, self.TZ).hour, 7)
        # China has no DST, so its cadence never changes.
        self.assertEqual(
            next_occurrence("cron", "0 7 * * *", after=shanghai, tz_name="Asia/Shanghai")
            - shanghai,
            24 * 3600,
        )

    def test_reserve_due_advances_over_the_switch_day_by_wall_clock(self):
        """End to end through the store: the DST day is 23 hours long, and the
        following slot is still 07:00 local."""
        scheduler = Scheduler(self.store, self.TZ, frozenset({"chat"}))
        march7 = ts("2026-03-07T07:00:00", self.TZ)
        self.seed(march7, kind="cron", value="0 7 * * *")

        reserved = scheduler.reserve_due(march7)
        self.assertEqual(len(reserved), 1)
        self.assertEqual(
            self.store.list_jobs("42")[0]["next_run"],
            ts("2026-03-08T07:00:00", self.TZ),
        )
        # And it does not come due again early on the short day.
        self.assertEqual(scheduler.reserve_due(march7 + 22 * 3600), [])
        self.assertEqual(len(scheduler.reserve_due(march7 + 23 * 3600)), 1)

    def test_count_missed_reports_a_capped_number_of_skipped_slots(self):
        start = ts("2026-03-07T07:00:00", self.TZ)
        now = ts("2026-03-10T12:00:00", self.TZ)
        self.assertEqual(
            count_missed("cron", "0 7 * * *", since=start, now=now, tz_name=self.TZ), 3
        )
        self.assertEqual(
            count_missed("every", "300", since=now - 1200, now=now, tz_name=self.TZ), 4
        )
        self.assertEqual(count_missed("every", "300", since=now, now=now, tz_name=self.TZ), 0)
        self.assertEqual(
            count_missed("cron", "0 7 * * *", since=start, now=now, tz_name=self.TZ, limit=2),
            2,
        )


class MisfireTests(SchedulingTestCase):
    def test_a_periodic_job_within_grace_is_coalesced_into_one_late_run(self):
        """Down for a couple of minutes is not a reason to drop the post; it is
        also not a reason to post it twice."""
        scheduler = Scheduler(self.store, "Asia/Shanghai", misfire_grace=300)
        self.seed(1000)

        deferred = scheduler.skip_missed(now=1100)
        self.assertEqual([row["config_key"] for row in deferred], ["j"])
        self.assertEqual(self.run_count(), 0)
        self.assertEqual(self.store.list_jobs("42")[0]["enabled"], 1)

        self.assertEqual(len(scheduler.reserve_due(1100)), 1)
        self.assertEqual(self.run_count(), 1)
        self.assertEqual(scheduler.reserve_due(1100), [])

    def test_a_periodic_job_past_grace_is_skipped_and_resumes_on_cadence(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai", misfire_grace=300)
        now = ts("2026-03-10T12:00:00", "Asia/Shanghai")
        start = ts("2026-03-07T07:00:00", "Asia/Shanghai")
        self.seed(start, kind="cron", value="0 7 * * *")

        self.assertEqual(scheduler.skip_missed(now=now), [])
        job = self.store.list_jobs("42")[0]
        self.assertEqual(job["enabled"], 1)
        self.assertEqual(job["next_run"], ts("2026-03-11T07:00:00", "Asia/Shanghai"))
        run = self.last_run()
        self.assertEqual(run["status"], "skipped")
        # Three slots were covered by one skip, and none of them was replayed.
        self.assertIn("3 occurrence", run["detail"])
        self.assertEqual(self.run_count(), 1)
        self.assertEqual(scheduler.reserve_due(now), [])

    def test_a_missed_one_shot_is_never_replayed_even_within_grace(self):
        """The gap does not apply to `at`: a reminder two minutes late is still
        a reminder for a moment that has passed."""
        scheduler = Scheduler(self.store, "Asia/Shanghai", misfire_grace=86400)
        self.seed(900, kind="at", value="2026-03-01T09:00:00+08:00")

        self.assertEqual(scheduler.skip_missed(now=1030), [])
        job = self.store.list_jobs("42")[0]
        self.assertEqual(job["enabled"], 0)
        self.assertEqual(self.last_run()["status"], "skipped")
        self.assertEqual(scheduler.reserve_due(1030), [])


class TimeoutTests(SchedulingTestCase):
    def execute(self, job: dict, callback, scheduler: Scheduler) -> dict:
        asyncio.run(scheduler._execute(callback, job))
        return self.job_row(job["run_id"])

    def reserved(self, scheduler: Scheduler, now: int = 1000) -> dict:
        self.seed(now - 10)
        return scheduler.reserve_due(now)[0]

    def test_a_stuck_handler_fails_instead_of_holding_the_run_forever(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai", job_timeout=0.05)
        job = self.reserved(scheduler)

        async def stuck(_job):
            await asyncio.sleep(30)

        run = self.execute(job, stuck, scheduler)
        self.assertEqual(run["status"], "failed")
        self.assertIn("timed out", run["detail"])
        self.assertIsNotNone(run["finished_at"])

    def test_a_timeout_is_not_recorded_as_a_skip(self):
        """A handler that declines is skipped. A handler that hangs is broken."""
        scheduler = Scheduler(self.store, "Asia/Shanghai", job_timeout=0.05)
        job = self.reserved(scheduler)

        async def stuck(_job):
            await asyncio.sleep(30)

        self.assertEqual(self.execute(job, stuck, scheduler)["status"], "failed")

        other = self.reserved(scheduler, now=2000)

        async def decline(_job):
            raise JobSkipped("quiet hours")

        run = self.execute(other, decline, scheduler)
        self.assertEqual((run["status"], run["detail"]), ("skipped", "quiet hours"))

    def test_a_handler_that_raises_is_failed_not_skipped(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        job = self.reserved(scheduler)

        async def broken(_job):
            raise RuntimeError("model is down")

        run = self.execute(job, broken, scheduler)
        self.assertEqual(run["status"], "failed")
        self.assertIn("model is down", run["detail"])

    def test_a_finished_handler_succeeds(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        job = self.reserved(scheduler)

        async def fine(_job):
            return None

        run = self.execute(job, fine, scheduler)
        self.assertEqual(run["status"], "succeeded")
        self.assertIsNotNone(run["finished_at"])


class IdempotencyTests(SchedulingTestCase):
    def test_the_same_run_is_never_reserved_twice(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        self.seed(1000)
        first = scheduler.reserve_due(1000)
        self.assertEqual(len(first), 1)
        self.assertEqual(scheduler.reserve_due(1000), [])
        self.assertEqual(self.run_count(), 1)

    def test_a_crash_after_sending_before_finishing_is_never_replayed(self):
        """The reservation moves next_run in the same transaction that opens
        the run, so the slot cannot be handed out again — the outcome is
        unknown, and that is recorded as such rather than guessed."""
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        self.seed(1000)
        run_id = scheduler.reserve_due(1000)[0]["run_id"]
        # The process dies here: the handler sent its message, but no finish().

        restarted = Scheduler(self.store, "Asia/Shanghai")
        restarted.reconcile_interrupted(now=1030)
        self.assertEqual(self.job_row(run_id)["status"], "interrupted")

        replayed = [
            row["run_id"] for now in range(1030, 1300, 15) for row in restarted.reserve_due(now)
        ]
        self.assertNotIn(run_id, replayed)
        self.assertEqual(replayed, [])
        self.assertEqual(self.run_count(), 1)

    def test_a_settled_run_cannot_be_rewritten_by_a_late_writer(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        self.seed(1000)
        run_id = scheduler.reserve_due(1000)[0]["run_id"]

        scheduler.reconcile_interrupted(now=1030)
        scheduler.finish(run_id, "succeeded")
        self.assertEqual(self.job_row(run_id)["status"], "interrupted")

    def test_only_a_running_row_can_be_finished(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        self.seed(1000)
        job = scheduler.reserve_due(1000)[0]

        async def fine(_job):
            return None

        asyncio.run(scheduler._execute(fine, job))
        self.assertEqual(self.job_row(job["run_id"])["status"], "succeeded")
        # A second, contradictory write cannot undo the recorded outcome.
        scheduler.finish(job["run_id"], "failed", "late writer")
        self.assertEqual(self.job_row(job["run_id"])["status"], "succeeded")


class LeaseTests(SchedulingTestCase):
    def test_only_one_instance_holds_the_scheduler_lease(self):
        a = Scheduler(self.store, "Asia/Shanghai", instance_id="a", lease_ttl=60)
        b = Scheduler(self.store, "Asia/Shanghai", instance_id="b", lease_ttl=60)

        self.assertTrue(a._hold_lease(1000))
        self.assertFalse(b._hold_lease(1000))
        self.assertEqual(self.store.scheduler_lease_owner(1000), "a")

        # A live holder renews every tick and keeps the lease.
        self.assertTrue(a._hold_lease(1050))
        self.assertFalse(b._hold_lease(1065))
        self.assertEqual(self.store.scheduler_lease_owner(1065), "a")

        # A dead holder stops renewing, and the lease expires into the other's
        # hands — no operator action, no duplicate posts in the meantime.
        self.assertEqual(self.store.scheduler_lease_owner(1111), None)
        self.assertTrue(b._hold_lease(1111))
        self.assertFalse(a._hold_lease(1111))

    def test_releasing_the_lease_hands_it_over_immediately(self):
        a = Scheduler(self.store, "Asia/Shanghai", instance_id="a")
        b = Scheduler(self.store, "Asia/Shanghai", instance_id="b")
        self.assertTrue(a._hold_lease(1000))
        a._release_lease()
        self.assertTrue(b._hold_lease(1000))

    def test_single_instance_mode_takes_no_lease_at_all(self):
        """Deployment today is one process. It must not pay for the feature."""
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        self.assertTrue(scheduler._hold_lease(1000))
        self.assertIsNone(self.store.scheduler_lease_owner(1000))

    def test_a_run_lease_lets_another_process_recover_a_dead_run(self):
        a = Scheduler(self.store, "Asia/Shanghai", instance_id="a", lease_ttl=60)
        self.seed(1000)
        job = a.reserve_due(1000)[0]
        self.assertTrue(self.store.claim_run_lease(job["run_id"], "a", 1000, 60))
        self.assertEqual(self.store.expire_run_leases(1059), 0)

        # A restarts; its run lease had expired, so the row can be settled
        # rather than sitting "running" for the life of the database.
        self.assertEqual(self.store.expire_run_leases(1060), 1)
        run = self.job_row(job["run_id"])
        self.assertEqual((run["status"], run["detail"]), ("abandoned", "lease expired"))

    def test_a_multi_instance_restart_only_reclaims_its_own_runs(self):
        a = Scheduler(self.store, "Asia/Shanghai", instance_id="a", lease_ttl=60)
        self.seed(1000)
        job = a.reserve_due(1000)[0]
        self.store.claim_run_lease(job["run_id"], "a", 1000, 60)

        b = Scheduler(self.store, "Asia/Shanghai", instance_id="b", lease_ttl=60)
        b.reconcile_interrupted(now=1010)
        # a's run is still leased and untouched; b's own run would be reclaimed.
        self.assertEqual(self.job_row(job["run_id"])["status"], "running")

        b.reconcile_interrupted(now=1061)
        self.assertEqual(self.job_row(job["run_id"])["status"], "abandoned")


class PayloadTests(SchedulingTestCase):
    class ReminderJob:
        action = "reminder"

        def suggested_jobs(self):
            return []

        def validate_payload(self, payload):
            if not payload.get("text"):
                raise ValueError("text is required")
            if len(str(payload["text"])) > 20:
                raise ValueError("text must be 20 characters or fewer")

        async def run(self, bot, job):
            raise AssertionError("not run in these tests")

    def scheduler(self, **kwargs) -> Scheduler:
        registry = JobHandlerRegistry()
        registry.register(self.ReminderJob())
        return Scheduler(
            self.store,
            "Asia/Shanghai",
            registry.actions(),
            payload_validators=registry.payload_validators(),
            **kwargs,
        )

    def test_a_job_carries_a_typed_payload_through_to_the_handler(self):
        path = self.schedules(
            {
                "id": "r",
                "group_id": "42",
                "kind": "cron",
                "value": "0 9 * * *",
                "action": "reminder",
                "payload": {"text": "喝水", "times": 3},
            }
        )
        self.scheduler().sync_config(path, frozenset({"42"}))
        job = self.store.list_jobs("42")[0]
        self.assertEqual(job["payload"], {"text": "喝水", "times": 3})
        self.assertEqual(job["spec_version"], JOB_SPEC_VERSION)

        # What a handler actually sees: the typed view, not the prompt string.
        runner = JobRunner(
            SimpleNamespace(
                policy=None,
                agent=None,
                conversations=None,
                activity=None,
                sender=None,
                last_reply={},
            ),
            JobHandlerRegistry(),
        )
        spec = runner.job_spec(job)
        self.assertIsInstance(spec, JobSpec)
        self.assertEqual(spec.payload["text"], "喝水")
        self.assertEqual(spec.action, "reminder")
        self.assertEqual(spec.key, "r")

    def test_a_payload_the_action_rejects_fails_at_startup_and_names_the_job(self):
        path = self.schedules(
            {
                "id": "r",
                "group_id": "42",
                "kind": "cron",
                "value": "0 9 * * *",
                "action": "reminder",
                "payload": {},
            }
        )
        with self.assertRaisesRegex(ValueError, "job 'r' has an invalid payload"):
            self.scheduler().sync_config(path, frozenset({"42"}))
        # Nothing was written, so nothing can fire at 9am.
        self.assertEqual(self.store.list_jobs("42"), [])

    def test_a_payload_must_be_a_small_json_object(self):
        oversized = {"text": "x" * 5000}
        for payload, expected in ((oversized, "exceeds"), ("nope", "must be an object")):
            with self.subTest(payload=type(payload).__name__):
                path = self.schedules(
                    {
                        "id": "r",
                        "group_id": "42",
                        "kind": "cron",
                        "value": "0 9 * * *",
                        "action": "reminder",
                        "payload": payload,
                    }
                )
                with self.assertRaisesRegex(ValueError, expected):
                    self.scheduler().sync_config(path, frozenset({"42"}))

    def test_changing_only_the_payload_reschedules_the_job(self):
        entry = {
            "id": "r",
            "group_id": "42",
            "kind": "cron",
            "value": "0 9 * * *",
            "action": "reminder",
            "payload": {"text": "喝水"},
        }
        path = self.schedules(entry)
        scheduler = self.scheduler()
        scheduler.sync_config(path, frozenset({"42"}))
        self.store.db.execute("UPDATE jobs SET next_run=12345")
        scheduler.sync_config(path, frozenset({"42"}))
        # Same payload, same schedule: the operator's next_run is left alone.
        self.assertEqual(self.store.list_jobs("42")[0]["next_run"], 12345)

        entry["payload"] = {"text": "睡觉"}
        path = self.schedules(entry)
        scheduler.sync_config(path, frozenset({"42"}))
        job = self.store.list_jobs("42")[0]
        self.assertEqual(job["payload"], {"text": "睡觉"})
        self.assertNotEqual(job["next_run"], 12345)

    def test_a_payload_without_a_validator_is_only_checked_as_an_envelope(self):
        """Actions with no parameters keep working exactly as before."""
        path = self.schedules(
            {
                "id": "morning",
                "group_id": "42",
                "kind": "cron",
                "value": "0 9 * * *",
                "prompt": "早安",
                "payload": {"anything": [1, 2, 3]},
            }
        )
        Scheduler(self.store, "Asia/Shanghai").sync_config(path, frozenset({"42"}))
        self.assertEqual(
            self.store.list_jobs("42")[0]["payload"], {"anything": [1, 2, 3]}
        )


class StartupCheckTests(SchedulingTestCase):
    def test_a_row_written_by_a_newer_build_stops_startup(self):
        self.seed(1000)
        self.store.db.execute("UPDATE jobs SET spec_version=99 WHERE config_key='j'")
        with self.assertRaisesRegex(ValueError, "spec version 99"):
            Scheduler(self.store, "Asia/Shanghai").sync_config(
                self.schedules(), frozenset({"42"})
            )

    def test_an_enabled_job_whose_extension_is_gone_stops_startup(self):
        """The deployer trimmed BOT_EXTENSIONS, but a row is still enabled and
        still expects an action nobody provides. Say which job."""
        self.store.db.execute(
            "INSERT INTO jobs(config_key,group_id,schedule_kind,schedule_value,"
            "prompt,action,next_run,enabled,created_by,created_at) "
            "VALUES('orphan','42','cron','0 9 * * *','p','rollcall',99999,1,'operator',0)"
        )
        with self.assertRaisesRegex(
            ValueError, "job 'orphan' is enabled and wants action 'rollcall'"
        ):
            Scheduler(self.store, "Asia/Shanghai", frozenset({"chat"})).sync_config(
                self.schedules(), frozenset({"42"})
            )

    def test_a_disabled_row_with_a_removed_action_is_left_alone(self):
        """That is what a stale handler suggestion looks like after trimming
        BOT_EXTENSIONS, and it is harmless until someone enables it."""
        self.store.db.execute(
            "INSERT INTO jobs(config_key,group_id,schedule_kind,schedule_value,"
            "prompt,action,next_run,enabled,created_by,created_at) "
            "VALUES('stale','42','cron','0 9 * * *','p','rollcall',99999,0,'handler',0)"
        )
        Scheduler(self.store, "Asia/Shanghai", frozenset({"chat"})).sync_config(
            self.schedules(), frozenset({"42"})
        )
        self.assertEqual(len(self.store.all_jobs()), 1)

    def test_a_removed_extension_names_the_job_it_breaks(self):
        """The friendly message from the config path is unchanged."""
        path = self.schedules(
            {
                "id": "p",
                "group_id": "42",
                "kind": "cron",
                "value": "0 7 * * *",
                "action": "poster",
                "prompt": "",
            }
        )
        with self.assertRaisesRegex(
            ValueError, "job 'p' wants action 'poster'.*BOT_EXTENSIONS"
        ):
            Scheduler(self.store, "Asia/Shanghai", frozenset({"chat"})).sync_config(
                path, frozenset({"42"})
            )

    def test_an_unknown_action_at_dispatch_is_skipped_not_raised(self):
        """Startup should have caught it; if not, the group stays clean."""
        registry = JobHandlerRegistry()
        with self.assertRaises(JobSkipped):
            registry.get("missing")
        self.assertEqual(registry.actions(), frozenset())


class CompatibilityTests(SchedulingTestCase):
    LEGACY_SCHEMA = """
        CREATE TABLE jobs (
          id INTEGER PRIMARY KEY, group_id TEXT NOT NULL,
          schedule_kind TEXT NOT NULL, schedule_value TEXT NOT NULL,
          prompt TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          next_run INTEGER NOT NULL, created_by TEXT NOT NULL,
          created_at INTEGER NOT NULL, config_key TEXT UNIQUE,
          action TEXT NOT NULL DEFAULT 'chat'
        );
        CREATE TABLE job_runs (
          id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
          status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
          started_at INTEGER NOT NULL, finished_at INTEGER
        );
    """

    def legacy_database(self) -> Path:
        path = self.root / "legacy.sqlite3"
        raw = sqlite3.connect(path)
        raw.executescript(self.LEGACY_SCHEMA)
        raw.execute(
            "INSERT INTO jobs(config_key,group_id,schedule_kind,schedule_value,"
            "prompt,action,next_run,enabled,created_by,created_at) "
            "VALUES('j','42','cron','0 9 * * *','早安','chat',555,1,'config',1)"
        )
        raw.execute("INSERT INTO job_runs(job_id,status,started_at) VALUES(1,'succeeded',5)")
        raw.commit()
        raw.close()
        return path

    def test_an_existing_database_gains_the_new_columns_in_place(self):
        path = self.legacy_database()
        store = Store(path)
        try:
            job = store.list_jobs("42")[0]
            self.assertEqual(job["payload"], {})
            self.assertEqual(job["spec_version"], JOB_SPEC_VERSION)
            run = store.job_run(1)
            self.assertEqual(run["attempt"], 1)
            self.assertIsNone(run["lease_owner"])
            self.assertIsNone(run["lease_expires_at"])
            self.assertEqual(run["status"], "succeeded")
        finally:
            store.db.close()

    def test_a_legacy_row_is_not_treated_as_edited_on_the_first_sync(self):
        """Upgrading must not reset next_run or resurrect a finished one-shot
        just because the row has no payload column yet."""
        path = self.legacy_database()
        store = Store(path)
        try:
            schedules = self.schedules(
                {
                    "id": "j",
                    "group_id": "42",
                    "kind": "cron",
                    "value": "0 9 * * *",
                    "prompt": "早安",
                }
            )
            Scheduler(store, "Asia/Shanghai").sync_config(schedules, frozenset({"42"}))
            job = store.list_jobs("42")[0]
            self.assertEqual(job["next_run"], 555)
            self.assertEqual(job["enabled"], 1)
        finally:
            store.db.close()

    def test_a_legacy_payload_column_reads_back_as_an_empty_dict(self):
        self.seed(1000)
        job = self.store.list_jobs("42")[0]
        self.assertEqual(job["payload"], {})
        self.assertEqual(JobSpec.from_row(job).payload, {})


if __name__ == "__main__":
    unittest.main()
