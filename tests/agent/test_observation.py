"""Post-reply observation contracts, executable independently of the Agent loop."""
from __future__ import annotations

import asyncio
import importlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from qqbot.actions.replies.models import ReplyDraft
from qqbot.agent.service import BotPolicy, ConversationService
from qqbot.contracts.models import MessageEvent
from qqbot.infrastructure.storage.database import SqliteDatabase
from qqbot.infrastructure.storage.observations import ObservationOutbox


def event(identity="e1", group="42"):
    return MessageEvent(identity, f"group:{group}" if group else "private:7",
                        group, "7", "小明", "你好", (), True, (), 0)


class Observer:
    def __init__(self, *, fail=False, hold=None):
        self.seen = []
        self.drafts = []
        self.fail = fail
        self.hold = hold
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def observe(self, incoming, reply):
        self.started.set()
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.fail:
                raise ValueError("broken consumer")
            self.seen.append((incoming.scope, incoming.event_id, reply))
        except asyncio.CancelledError:
            self.cancelled.set()
            raise

    async def observe_draft(self, incoming, reply, draft):
        self.drafts.append(draft)
        await self.observe(incoming, reply)


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(Path(self.temporary.name) / "observations.sqlite3")
        self.outbox = ObservationOutbox(self.db)
        self.dispatchers = []

    async def asyncTearDown(self):
        for dispatcher in self.dispatchers:
            await dispatcher.aclose(timeout=0.01)
        self.db.close()
        self.temporary.cleanup()

    def new(self, **options):
        name = "qqbot.agent.observation"
        self.assertIsNotNone(importlib.util.find_spec(name),
                             "Observation lifecycle needs its own module")
        cls = getattr(importlib.import_module(name), "ObservationDispatcher", None)
        self.assertIsNotNone(cls, "The standalone observation boundary is missing")
        dispatcher = cls(**options)
        self.dispatchers.append(dispatcher)
        return dispatcher

    async def wait_until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0)

    async def test_group_and_private_observers_keep_the_original_scope(self):
        observer = Observer()
        dispatcher = self.new(observers=(observer,))
        dispatcher.dispatch(event("g"), "群聊")
        dispatcher.dispatch(event("p", None), "私聊")
        await dispatcher.aclose()
        self.assertEqual(observer.seen, [("group:42", "g", "群聊"), ("private:7", "p", "私聊")])
        self.assertEqual(dispatcher.stats.completed, 2)

    async def test_affection_is_group_only(self):
        affection = Observer()
        dispatcher = self.new(affection=affection, affection_enabled=True)
        dispatcher.dispatch(event("g"), "群聊")
        dispatcher.dispatch(event("p", None), "私聊")
        await dispatcher.aclose()
        self.assertEqual(affection.seen, [("group:42", "g", "群聊")])

    async def test_disabled_affection_does_not_queue_work(self):
        affection = Observer()
        dispatcher = self.new(affection=affection, affection_enabled=False)
        dispatcher.dispatch(event(), "hi")
        self.assertEqual(dispatcher.pending, 0)
        self.assertEqual(dispatcher.stats.enqueued, 0)

    async def test_structured_draft_reaches_draft_capable_observers(self):
        observer = Observer()
        dispatcher = self.new(observers=(observer,))
        draft = ReplyDraft("hi", state_observations={"mood": "happy"})
        dispatcher.dispatch(event(), "hi", draft)
        await dispatcher.aclose()
        self.assertEqual(observer.drafts, [draft])

    async def test_plain_observer_accepts_structured_draft(self):
        seen = []
        async def observe(incoming, reply):
            seen.append(incoming.event_id)
        dispatcher = self.new(observers=(SimpleNamespace(observe=observe),))
        dispatcher.dispatch(event(), "hi", ReplyDraft("hi", state_observations={}))
        await dispatcher.aclose()
        self.assertEqual(seen, ["e1"])

    async def test_duplicate_key_is_not_run_twice(self):
        observer = Observer()
        dispatcher = self.new()
        self.assertTrue(dispatcher.submit("a", observer, event(), "hi"))
        self.assertFalse(dispatcher.submit("a", observer, event(), "hi"))
        await dispatcher.aclose()
        self.assertEqual(len(observer.seen), 1)
        self.assertEqual(dispatcher.stats.deduplicated, 1)

    async def test_full_queue_drops_and_claims_the_key(self):
        observer = Observer()
        dispatcher = self.new(backlog=1)
        self.assertTrue(dispatcher.submit("a", observer, event("a"), "hi"))
        self.assertFalse(dispatcher.submit("b", observer, event("b"), "hi"))
        self.assertEqual(dispatcher.pending, 1)
        await dispatcher.aclose()
        self.assertEqual(dispatcher.stats.dropped, 1)
        self.assertEqual(observer.seen[0][1], "a")
        self.assertFalse(dispatcher.submit("b", observer, event("b"), "hi"))

    async def test_dedupe_window_evicts_oldest_key(self):
        observer = Observer()
        dispatcher = self.new(dedupe=1)
        dispatcher.submit("a", observer, event("a"), "hi")
        await self.wait_until(lambda: dispatcher.stats.completed == 1)
        dispatcher.submit("b", observer, event("b"), "hi")
        await self.wait_until(lambda: dispatcher.stats.completed == 2)
        self.assertTrue(dispatcher.submit("a", observer, event("a2"), "hi"))
        await dispatcher.aclose()
        self.assertEqual([row[1] for row in observer.seen], ["a", "b", "a2"])

    async def test_configured_workers_can_progress_independently(self):
        slow = Observer(hold=asyncio.Event())
        fast = Observer()
        dispatcher = self.new(workers=2)
        dispatcher.submit("slow", slow, event(), "hi")
        dispatcher.submit("fast", fast, event(), "hi")
        await asyncio.wait_for(slow.started.wait(), 1)
        await self.wait_until(lambda: bool(fast.seen))
        slow.hold.set()
        await dispatcher.aclose()
        self.assertEqual(dispatcher.stats.completed, 2)

    async def test_failure_does_not_stop_the_worker_or_retry(self):
        broken, healthy = Observer(fail=True), Observer()
        dispatcher = self.new()
        dispatcher.submit("bad", broken, event(), "hi")
        dispatcher.submit("ok", healthy, event(), "hi")
        await self.wait_until(lambda: dispatcher.stats.completed == 1)
        self.assertFalse(dispatcher.submit("bad", broken, event(), "hi"))
        self.assertEqual(dispatcher.stats.failed, 1)
        self.assertEqual(len(healthy.seen), 1)

    async def test_close_drains_fifo_and_is_repeatable(self):
        observer = Observer()
        dispatcher = self.new()
        for index in range(3):
            dispatcher.submit(str(index), observer, event(str(index)), "hi")
        await dispatcher.aclose()
        await dispatcher.aclose()
        self.assertEqual([row[1] for row in observer.seen], ["0", "1", "2"])

    async def test_close_timeout_cancels_hanging_consumer(self):
        observer = Observer(hold=asyncio.Event())
        dispatcher = self.new()
        dispatcher.submit("a", observer, event(), "hi")
        await asyncio.wait_for(observer.started.wait(), 1)
        await asyncio.wait_for(dispatcher.aclose(timeout=0.01), 1)
        self.assertTrue(observer.cancelled.is_set())
        self.assertEqual(dispatcher.stats.failed, 0)

    async def test_cancelled_close_still_awaits_worker_cleanup(self):
        observer = Observer(hold=asyncio.Event())
        dispatcher = self.new()
        dispatcher.submit("a", observer, event(), "hi")
        await asyncio.wait_for(observer.started.wait(), 1)
        closing = asyncio.create_task(dispatcher.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertTrue(observer.cancelled.is_set())
        self.assertFalse(dispatcher.submit("b", observer, event(), "hi"))

    async def test_close_before_any_work_refuses_admission(self):
        dispatcher = self.new()
        await dispatcher.aclose()
        self.assertFalse(dispatcher.submit("a", Observer(), event(), "hi"))
        self.assertEqual(dispatcher.pending, 0)

    async def test_durable_storage_bounds_and_deduplicates_admission(self):
        dispatcher = self.new(store=self.outbox, backlog=1)
        observer = Observer()
        self.assertTrue(dispatcher.submit("a", observer, event(), "hi"))
        self.assertFalse(dispatcher.submit("a", observer, event(), "hi"))
        self.assertFalse(dispatcher.submit("b", observer, event(), "hi"))
        self.assertFalse(dispatcher.submit("huge", observer, event(), "x"*70000))
        self.assertEqual(dispatcher.pending, 1)
        self.assertEqual(dispatcher.stats.deduplicated, 1)
        self.assertEqual(dispatcher.stats.dropped, 2)

    async def test_durable_roundtrip_preserves_scope_and_draft(self):
        observer = Observer()
        dispatcher = self.new(observers=(observer,), store=self.outbox)
        draft = ReplyDraft("hi", state_observations={"mood": "happy"})
        dispatcher.dispatch(event("p", None), "hi", draft)
        running = asyncio.create_task(dispatcher.run_durable())
        await self.wait_until(lambda: dispatcher.stats.completed == 1)
        await dispatcher.aclose()
        self.assertTrue(running.done())
        self.assertEqual(observer.seen, [("private:7", "p", "hi")])
        self.assertEqual(observer.drafts[0].state_observations, draft.state_observations)
        self.assertEqual(dispatcher.pending, 0)

    async def test_durable_error_marks_failed_and_continues(self):
        observer = Observer(fail=True)
        dispatcher = self.new(observers=(observer,), store=self.outbox)
        dispatcher.dispatch(event(), "hi")
        running = asyncio.create_task(dispatcher.run_durable())
        await self.wait_until(lambda: dispatcher.stats.failed == 1)
        observer.fail = False
        dispatcher.dispatch(event("next"), "ok")
        await self.wait_until(lambda: dispatcher.stats.completed == 1)
        await dispatcher.aclose()
        self.assertTrue(running.done())
        self.assertEqual(dispatcher.pending, 0)

    async def test_durable_close_cancels_inflight_consumer(self):
        observer = Observer(hold=asyncio.Event())
        dispatcher = self.new(observers=(observer,), store=self.outbox)
        dispatcher.dispatch(event(), "hi")
        running = asyncio.create_task(dispatcher.run_durable())
        await asyncio.wait_for(observer.started.wait(), 1)
        await asyncio.wait_for(dispatcher.aclose(timeout=0.01), 1)
        self.assertTrue(running.done())
        self.assertTrue(observer.cancelled.is_set())
        self.assertEqual(dispatcher.stats.failed, 0)
        self.assertEqual(dispatcher.pending, 1)

    async def test_recovery_does_not_replay_non_idempotent_consumer(self):
        observer = Observer()
        identity = type(observer).__module__ + "." + type(observer).__qualname__
        self.outbox.enqueue("interrupted", identity, asdict(event()), "old", None, replayable=False)
        self.outbox.lease((identity,))
        self.outbox.enqueue("next", identity, asdict(event("next")), "new", None, replayable=False)
        dispatcher = self.new(observers=(observer,), store=self.outbox)
        running = asyncio.create_task(dispatcher.run_durable())
        await self.wait_until(lambda: dispatcher.stats.completed == 1)
        await dispatcher.aclose()
        self.assertTrue(running.done())
        self.assertEqual(observer.seen, [("group:42", "next", "new")])

    async def test_no_store_durable_loop_returns_without_starting_workers(self):
        dispatcher = self.new()
        await dispatcher.run_durable()
        self.assertEqual(dispatcher.pending, 0)

    async def test_closed_durable_loop_does_not_recover_or_consume(self):
        observer = Observer()
        dispatcher = self.new(observers=(observer,), store=self.outbox)
        dispatcher.dispatch(event(), "hi")
        await dispatcher.aclose()
        await dispatcher.run_durable()
        self.assertEqual(observer.seen, [])
        self.assertEqual(dispatcher.pending, 1)

    async def test_service_delegates_to_one_observation_owner(self):
        owner_type = type(self.new())
        service = ConversationService(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                                      SimpleNamespace(), SimpleNamespace(),
                                      BotPolicy(frozenset(), 8, "UTC", False), restore_last_reply=False)
        try:
            self.assertIsInstance(getattr(service, "post_reply", None), owner_type)
            self.assertIs(service.observations, service.post_reply.stats)
            self.assertFalse(any(name in vars(service) for name in ("_queue", "_workers", "_seen", "_durable_handlers")))
            observer = Observer()
            self.assertTrue(service.submit_observation("test", observer, event(), "hi"))
            await service.aclose()
            self.assertEqual(service.observations.completed, 1)
        finally:
            await service.aclose()

    async def test_durable_payload_uses_original_handler_identity(self):
        observer = Observer()
        dispatcher = self.new(affection=observer, affection_enabled=True, store=self.outbox)
        dispatcher.dispatch(event(), "hi", ReplyDraft("hi", state_observations={}))
        identity = type(observer).__module__ + "." + type(observer).__qualname__
        row = self.outbox.lease((identity,))
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["draft_json"])["state_observations"], {})
        self.assertEqual(row["replayable"], 0)
