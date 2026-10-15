"""Maintenance acceptance: plain ports first, then the actual service boundary."""
from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from qqbot.agent.conversation.maintenance import ConversationMaintenance
from qqbot.agent.fairness import ScopeWorkBudget
from qqbot.agent.service import ConversationService
from tests.support import bot_policy
from tests.agent.test_service import FakeAgent, FakeSender, RecordingObserver, event
from tests.support import Store


class MemoryWork:
    def __init__(self):
        self.calls = []
        self.cancelled = []
        self.active = 0
        self.maximum = 0
        self.hold = None
        self.fail = False

    async def extract(self, scope):
        self.calls.append(scope)
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.fail:
                raise RuntimeError("extraction failed")
        except asyncio.CancelledError:
            self.cancelled.append(scope)
            raise
        finally:
            self.active -= 1


class Compactor:
    def __init__(self):
        self.calls = []
        self.cancelled = []
        self.hold = None
        self.fail = False

    async def compact_if_needed(self, scope, current_event_id=""):
        self.calls.append((scope, current_event_id))
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.fail:
                raise RuntimeError("compaction failed")
            return True
        except asyncio.CancelledError:
            self.cancelled.append(scope)
            raise


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.counts = {}
        self.memory = MemoryWork()
        self.compactor = Compactor()
        self.owners = []
        self.logger = logging.getLogger("maintenance.acceptance")

    def new(self, **overrides):
        options = dict(message_count=lambda scope: self.counts.get(scope, 0),
                       extract_memory=self.memory.extract, extract_every=4,
                       compactor=self.compactor, logger=self.logger)
        options.update(overrides)
        owner = ConversationMaintenance(**options)
        self.owners.append(owner)
        return owner

    async def asyncTearDown(self):
        for owner in self.owners:
            await owner.aclose()

    async def test_invalid_intervals_and_concurrency_fail_at_construction(self):
        for settings in ({"extract_every": 0}, {"extract_every": -1},
                         {"extraction_concurrency": 0}, {"extraction_concurrency": -1}):
            with self.assertRaises(ValueError):
                self.new(**settings)

    async def test_empty_history_and_non_multiple_do_not_extract(self):
        owner = self.new()
        self.assertFalse(owner.maybe_extract("group:42"))
        self.counts["group:42"] = 3
        self.assertFalse(owner.maybe_extract("group:42"))
        self.assertEqual(self.memory.calls, [])

    async def test_exact_interval_schedules_asynchronously(self):
        owner = self.new()
        self.counts["group:42"] = 4
        self.assertTrue(owner.maybe_extract("group:42"))
        self.assertEqual(self.memory.calls, [])
        self.assertEqual(owner.pending_extractions, frozenset({"group:42"}))
        await until(lambda: not owner.pending_extractions)
        self.assertEqual(self.memory.calls, ["group:42"])

    async def test_same_scope_is_single_flight_even_before_task_starts(self):
        owner = self.new()
        self.counts["group:42"] = 4
        self.memory.hold = asyncio.Event()
        self.assertTrue(owner.maybe_extract("group:42"))
        self.assertFalse(owner.maybe_extract("group:42"))
        await until(lambda: bool(self.memory.calls))
        self.assertFalse(owner.maybe_extract("group:42"))
        self.memory.hold.set()
        await until(lambda: not owner.pending_extractions)
        self.counts["group:42"] = 8
        self.assertTrue(owner.maybe_extract("group:42"))
        await until(lambda: not owner.pending_extractions)
        self.assertEqual(self.memory.calls, ["group:42", "group:42"])

    async def test_group_and_private_scopes_remain_distinct(self):
        owner = self.new()
        self.counts.update({"group:7": 4, "private:7": 4})
        self.memory.hold = asyncio.Event()
        self.assertTrue(owner.maybe_extract("group:7"))
        self.assertTrue(owner.maybe_extract("private:7"))
        await until(lambda: len(self.memory.calls) == 2)
        self.assertEqual(set(self.memory.calls), {"group:7", "private:7"})
        self.assertEqual(owner.pending_extractions, frozenset({"group:7", "private:7"}))

    async def test_default_extraction_concurrency_is_two_across_scopes(self):
        owner = self.new()
        self.memory.hold = asyncio.Event()
        for index in range(5):
            scope = f"group:{index}"
            self.counts[scope] = 4
            self.assertTrue(owner.maybe_extract(scope))
        await until(lambda: self.memory.active == 2)
        self.assertEqual(len(self.memory.calls), 2)
        self.assertEqual(len(owner.pending_extractions), 5)
        self.memory.hold.set()
        await until(lambda: not owner.pending_extractions)
        self.assertEqual(len(self.memory.calls), 5)
        self.assertEqual(self.memory.maximum, 2)

    async def test_configured_extraction_limit_is_respected(self):
        owner = self.new(extraction_concurrency=1)
        self.memory.hold = asyncio.Event()
        self.counts.update({"group:42": 4, "private:7": 4})
        owner.maybe_extract("group:42")
        owner.maybe_extract("private:7")
        await until(lambda: self.memory.active == 1)
        self.assertEqual(len(self.memory.calls), 1)
        self.memory.hold.set()
        await until(lambda: not owner.pending_extractions)
        self.assertEqual(self.memory.maximum, 1)

    async def test_failed_extraction_logs_and_releases_scope_for_next_interval(self):
        owner = self.new()
        self.counts["group:42"] = 4
        self.memory.fail = True
        with self.assertLogs(self.logger, level="ERROR"):
            self.assertTrue(owner.maybe_extract("group:42"))
            await until(lambda: not owner.pending_extractions)
        self.assertEqual(self.memory.calls, ["group:42"])
        self.memory.fail = False
        self.counts["group:42"] = 8
        self.assertTrue(owner.maybe_extract("group:42"))
        await until(lambda: not owner.pending_extractions)
        self.assertEqual(len(self.memory.calls), 2)

    async def test_pending_scope_snapshots_cannot_change_internal_state(self):
        owner = self.new()
        self.counts["group:42"] = 4
        owner.maybe_extract("group:42")
        snapshot = owner.pending_extractions
        self.assertIsInstance(snapshot, frozenset)
        await until(lambda: not owner.pending_extractions)
        self.assertEqual(snapshot, frozenset({"group:42"}))
        self.assertEqual(owner.pending_extractions, frozenset())

    async def test_compaction_uses_scope_and_current_event_without_blocking(self):
        owner = self.new()
        self.assertTrue(owner.after_reply(event("delivered")))
        self.assertEqual(self.compactor.calls, [])
        self.assertEqual(owner.pending_compactions, frozenset({"group:42"}))
        await until(lambda: not owner.pending_compactions)
        self.assertEqual(self.compactor.calls, [("group:42", "delivered")])

    async def test_compaction_can_be_disabled(self):
        owner = self.new(compactor=None)
        self.assertFalse(owner.after_reply(event("1")))
        self.assertEqual(owner.pending_compactions, frozenset())

    async def test_operator_messages_do_not_consume_compaction_budget(self):
        owner = self.new()
        self.assertFalse(owner.after_reply(replace(event("op"), origin="operator")))
        for index in range(2):
            self.assertTrue(owner.after_reply(event(str(index))))
            await until(lambda: not owner.pending_compactions)
        self.assertFalse(owner.after_reply(event("third")))
        self.assertEqual(len(self.compactor.calls), 2)

    async def test_same_scope_compaction_is_single_flight(self):
        owner = self.new()
        self.compactor.hold = asyncio.Event()
        self.assertTrue(owner.after_reply(event("one")))
        self.assertFalse(owner.after_reply(event("two")))
        await until(lambda: bool(self.compactor.calls))
        self.assertFalse(owner.after_reply(event("three")))
        self.compactor.hold.set()
        await until(lambda: not owner.pending_compactions)
        self.assertTrue(owner.after_reply(event("four")))
        await until(lambda: not owner.pending_compactions)
        self.assertEqual(self.compactor.calls, [("group:42", "one"), ("group:42", "four")])

    async def test_compaction_budget_is_independent_for_group_and_private(self):
        now = [0.0]
        budget = ScopeWorkBudget(max_per_scope=1, window_seconds=10, clock=lambda: now[0])
        owner = self.new(compaction_budget=budget)
        group = event("g")
        private = replace(event("p"), group_id=None, scope="private:7")
        self.assertTrue(owner.after_reply(group))
        await until(lambda: not owner.pending_compactions)
        self.assertFalse(owner.after_reply(group))
        self.assertTrue(owner.after_reply(private))
        await until(lambda: not owner.pending_compactions)
        now[0] = 10
        self.assertTrue(owner.after_reply(group))
        await until(lambda: not owner.pending_compactions)
        self.assertEqual(self.compactor.calls, [("group:42", "g"), ("private:7", "p"), ("group:42", "g")])

    async def test_compaction_failure_is_advisory_and_has_no_automatic_retry(self):
        owner = self.new()
        self.compactor.fail = True
        with self.assertLogs(self.logger, level="ERROR"):
            self.assertTrue(owner.after_reply(event("bad")))
            await until(lambda: not owner.pending_compactions)
        self.assertEqual(len(self.compactor.calls), 1)
        self.compactor.fail = False
        self.assertTrue(owner.after_reply(event("next")))
        await until(lambda: not owner.pending_compactions)
        self.assertEqual(self.compactor.calls[-1], ("group:42", "next"))

    async def test_close_cancels_running_and_queued_extractions_and_compactions(self):
        owner = self.new(extraction_concurrency=1)
        self.memory.hold = asyncio.Event()
        self.compactor.hold = asyncio.Event()
        self.counts.update({"group:42": 4, "private:7": 4})
        owner.maybe_extract("group:42")
        owner.maybe_extract("private:7")
        owner.after_reply(event("g"))
        owner.after_reply(replace(event("p"), group_id=None, scope="private:7"))
        await until(lambda: self.memory.active == 1 and len(self.compactor.calls) == 2)
        await owner.aclose()
        self.assertEqual(self.memory.cancelled, ["group:42"])
        self.assertEqual(self.memory.calls, ["group:42"])
        self.assertEqual(set(self.compactor.cancelled), {"group:42", "private:7"})
        self.assertEqual(owner.pending_extractions, frozenset())
        self.assertEqual(owner.pending_compactions, frozenset())

    async def test_closed_maintenance_refuses_all_work(self):
        owner = self.new()
        self.counts["group:42"] = 4
        await owner.aclose()
        self.assertFalse(owner.maybe_extract("group:42"))
        self.assertFalse(owner.after_reply(event("1")))
        await owner.aclose()
        self.assertEqual(self.memory.calls, [])

    async def test_close_before_tasks_start_does_not_leak_scope_reservations(self):
        owner = self.new()
        self.counts["group:42"] = 4
        self.assertTrue(owner.maybe_extract("group:42"))
        self.assertTrue(owner.after_reply(event("1")))
        await owner.aclose()
        self.assertEqual(owner.pending_extractions, frozenset())
        self.assertEqual(owner.pending_compactions, frozenset())

    async def test_cancelled_and_concurrent_closers_wait_for_the_same_cleanup(self):
        started, cleaning, release, finished = (asyncio.Event() for _ in range(4))
        async def extract(scope):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.set()
        owner = self.new(extract_memory=extract)
        self.counts["group:42"] = 4
        self.assertTrue(owner.maybe_extract("group:42"))
        await asyncio.wait_for(started.wait(), 2)
        first = asyncio.create_task(owner.aclose())
        await asyncio.wait_for(cleaning.wait(), 2)
        second = asyncio.create_task(owner.aclose())
        try:
            await asyncio.sleep(0)
            self.assertFalse(second.done())
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(first.done())
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
        self.assertTrue(finished.is_set())
        self.assertTrue(first.cancelled())
        self.assertEqual(owner.pending_extractions, frozenset())


class MaintenanceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "bot.sqlite3")
        self.agent, self.sender, self.compactor = FakeAgent(), FakeSender(), Compactor()
        self.agent.conversation_compactor = self.compactor
        self.services = []

    def service(self, **options):
        policy = bot_policy(frozenset({"42"}), 2, "UTC", False, private_enabled=True)
        result = ConversationService(self.agent, self.store, self.store, self.store,
                                     self.sender, policy, restore_last_reply=False, **options)
        self.services.append(result)
        return result

    async def asyncTearDown(self):
        for service in self.services:
            await service.aclose(timeout=0.01)
        self.store.db.close()
        self.temporary.cleanup()

    async def test_service_has_one_maintenance_owner_and_no_old_task_state(self):
        service = self.service()
        self.assertIsInstance(getattr(service, "maintenance", None), ConversationMaintenance)
        self.assertFalse(set(vars(service)) & {"extracting", "extract_slots", "_extract_tasks", "_compaction_tasks", "_compaction_budget"})
        self.assertFalse(any(hasattr(service, name) for name in
                             ("_maybe_extract", "_extract_safe", "_schedule_compaction", "_compact_safe")))

    async def test_ingest_calls_maintenance_once_per_persisted_message(self):
        service = self.service()
        self.assertTrue(hasattr(service, "maintenance"), "service must delegate maintenance")
        seen = []
        original = service.maintenance.maybe_extract
        def record(scope):
            seen.append(scope)
            return original(scope)
        service.maintenance.maybe_extract = record
        self.assertTrue(await service.ingest(event("1", at_bot=False)))
        self.assertTrue(await service.ingest(event("2", at_bot=False)))
        self.assertFalse(await service.ingest(event("2", at_bot=False)))
        self.assertFalse(await service.ingest(event("blocked", group="99")))
        await until(lambda: self.agent.extractions == ["group:42"])
        self.assertEqual(seen, ["group:42", "group:42"])
        self.assertEqual(self.sender.sent, [])

    async def test_private_ingest_extracts_using_private_scope(self):
        service = self.service()
        incoming = replace(event("p1"), group_id=None, scope="private:7")
        await service.ingest(incoming)
        await service.ingest(replace(incoming, event_id="p2"))
        await until(lambda: bool(self.agent.extractions))
        self.assertEqual(self.agent.extractions, ["private:7"])

    async def test_successful_delivery_records_before_scheduling_compaction(self):
        service = self.service()
        self.assertTrue(hasattr(service, "maintenance"), "service must delegate maintenance")
        recorded = []
        original = service.maintenance.after_reply
        def record(incoming):
            recorded.append(self.store.recent(incoming.scope, 2)[-1]["role"])
            return original(incoming)
        service.maintenance.after_reply = record
        self.compactor.hold = asyncio.Event()
        self.assertEqual(await asyncio.wait_for(service.execute_turn(event("reply")), 2), "收到。")
        await until(lambda: bool(self.compactor.calls))
        self.assertEqual(recorded, ["assistant"])
        self.assertEqual(self.compactor.calls, [("group:42", "reply")])

    async def test_empty_or_failed_model_replies_do_not_schedule_compaction(self):
        service = self.service()
        self.agent.text = ""
        self.assertIsNone(await service.execute_turn(event("empty")))
        self.agent.fail = True
        with self.assertLogs("qqbot.agent.service", level="ERROR"):
            self.assertIsNone(await service.execute_turn(event("fail")))
        self.assertEqual(self.compactor.calls, [])

    async def test_send_failure_does_not_schedule_compaction(self):
        class BrokenSender(FakeSender):
            async def send(self, **kwargs):
                raise ConnectionError("send rejected")
        self.sender = BrokenSender()
        service = self.service()
        with self.assertRaises(ConnectionError):
            await service.execute_turn(event("send-fail"))
        self.assertEqual(self.compactor.calls, [])

    async def test_successful_private_reply_uses_private_compaction_scope(self):
        service = self.service()
        incoming = replace(event("p"), group_id=None, scope="private:7")
        self.assertEqual(await service.execute_turn(incoming), "收到。")
        await until(lambda: bool(self.compactor.calls))
        self.assertEqual(self.compactor.calls, [("private:7", "p")])

    async def test_cancelled_observation_drain_still_cleans_up_maintenance(self):
        observer = RecordingObserver(hang=True)
        service = self.service(observers=(observer,))
        self.compactor.hold = asyncio.Event()
        await service.execute_turn(event("g"))
        await until(lambda: observer.started == 1 and bool(self.compactor.calls))
        closing = asyncio.create_task(service.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertEqual(self.compactor.cancelled, ["group:42"])

    async def test_observation_shutdown_failure_does_not_leave_maintenance_running(self):
        service = self.service()
        self.compactor.hold = asyncio.Event()
        await service.execute_turn(event("g"))
        await until(lambda: bool(self.compactor.calls))
        original_close = service.post_reply.aclose
        service.post_reply.aclose = AsyncMock(side_effect=RuntimeError("close failed"))
        try:
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                await service.aclose()
            self.assertEqual(self.compactor.cancelled, ["group:42"])
        finally:
            service.post_reply.aclose = original_close
