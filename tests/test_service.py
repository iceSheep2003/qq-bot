"""Conversation lifecycle: ingest, reply decision, turn execution, observations.

Everything runs against fake ports and a real SQLite file; no NapCat, no model.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from qunbot.domain import MessageEvent
from qunbot.runtime.service import BotPolicy, ConversationService
from support import Store


class FakeAgent:
    """Replies with a canned text and records how many turns overlap."""

    def __init__(self, text: str = "收到。"):
        self.text = text
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0
        self.hold: asyncio.Event | None = None
        self.fail = False
        self.extractions: list[str] = []

    async def reply(self, event, *, proactive: bool = False):
        self.calls.append(event.event_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.hold is not None:
                await self.hold.wait()
            if self.fail:
                raise RuntimeError("model exploded")
            return SimpleNamespace(text=self.text)
        finally:
            self.active -= 1

    async def extract_memory(self, scope: str) -> None:
        self.extractions.append(scope)


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return {}

    async def react_to_message(self, message_id, emoji_id, **kwargs):
        self.sent.append({"reaction": emoji_id, "message_id": message_id, **kwargs})
        return {}


class AlwaysReact:
    def decide(self, _event):
        return SimpleNamespace(
            react=True, emoji_id="76", name="赞", reason="test"
        )


class RecordingObserver:
    """A post-reply observer that can be slow, can fail, and records calls."""

    def __init__(self, *, delay: float = 0.0, fail: bool = False, hang: bool = False):
        self.delay = delay
        self.fail = fail
        self.hang = hang
        self.started = 0
        self.seen: list[str] = []

    async def observe(self, event: MessageEvent, bot_reply: str) -> None:
        self.started += 1
        if self.hang:
            await asyncio.Event().wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("observer exploded")
        self.seen.append(event.event_id)


def event(message_id: str, *, group: str = "42", at_bot: bool = True, user: str = "7"):
    return MessageEvent(
        message_id, f"group:{group}", group, user, "小明", "你好", (), at_bot, (), 0
    )


async def wait_for(predicate, *, timeout: float = 2.0) -> None:
    """Let the loop run until predicate() is true."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "bot.sqlite3")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def policy(self, **overrides) -> BotPolicy:
        base = {
            "allowed_groups": frozenset({"42"}),
            "memory_extract_every": 8,
            "timezone": "Asia/Shanghai",
            "affection_auto_enabled": False,
            "private_enabled": False,
        }
        base.update(overrides)
        return BotPolicy(**base)

    def service(
        self,
        *,
        agent=None,
        sender=None,
        policy=None,
        affection=None,
        observers=(),
        restore_last_reply=True,
        **kwargs,
    ) -> ConversationService:
        return ConversationService(
            agent or FakeAgent(),
            self.store,
            self.store,
            self.store,
            sender or FakeSender(),
            policy or self.policy(),
            affection,
            None,
            tuple(observers),
            restore_last_reply=restore_last_reply,
            **kwargs,
        )

    # --- ingest ----------------------------------------------------------

    def test_a_group_message_is_stored_exactly_once(self):
        service = self.service()

        async def run():
            self.assertTrue(await service.ingest(event("1")))
            self.assertFalse(await service.ingest(event("1")))
            self.assertFalse(await service.ingest(event("1", at_bot=False)))

        asyncio.run(run())
        self.assertEqual(self.store.message_count("group:42"), 1)

    def test_non_allowlisted_group_and_disabled_private_are_not_stored(self):
        service = self.service()

        async def run():
            self.assertFalse(await service.ingest(event("1", group="99")))
            private = MessageEvent(
                "p1", "private:7", None, "7", "小明", "你好", (), False, (), 0
            )
            self.assertFalse(await service.ingest(private))

        asyncio.run(run())
        self.assertEqual(self.store.message_count("group:99"), 0)
        self.assertEqual(self.store.message_count("private:7"), 0)

    def test_an_empty_message_is_not_stored(self):
        service = self.service()
        blank = MessageEvent("1", "group:42", "42", "7", "小明", "", (), False, (), 0)
        self.assertFalse(asyncio.run(service.ingest(blank)))

    def test_a_plain_group_message_is_recorded_but_not_answered(self):
        sender, agent = FakeSender(), FakeAgent()
        service = self.service(agent=agent, sender=sender)
        asyncio.run(service.handle_message(event("1", at_bot=False)))
        self.assertEqual(sender.sent, [])
        self.assertEqual(agent.calls, [])
        self.assertEqual(self.store.message_count("group:42"), 1)

    def test_reaction_is_independent_from_text_reply_admission(self):
        sender, agent = FakeSender(), FakeAgent()
        service = self.service(
            agent=agent,
            sender=sender,
            reaction_policy=AlwaysReact(),
            reaction_sender=sender,
        )
        item = event("123", at_bot=False)
        item = MessageEvent(
            **{**item.__dict__, "platform_message_id": "123", "text": "学完了"}
        )
        asyncio.run(service.handle_message(item))
        self.assertEqual(
            sender.sent,
            [{"reaction": "76", "message_id": "123"}],
        )
        self.assertEqual(agent.calls, [])

    # --- turn execution --------------------------------------------------

    def test_one_group_is_serial_and_recorded_once(self):
        agent, sender = FakeAgent(), FakeSender()
        service = self.service(agent=agent, sender=sender)

        async def run():
            agent.hold = asyncio.Event()
            first = asyncio.create_task(service.handle_message(event("1")))
            second = asyncio.create_task(service.handle_message(event("2")))
            await wait_for(lambda: agent.active == 1)
            # The second turn must wait for the scope lock, not run alongside.
            await asyncio.sleep(0.02)
            self.assertEqual(agent.active, 1)
            agent.hold.set()
            await asyncio.gather(first, second)

        asyncio.run(run())
        self.assertEqual(agent.max_active, 1)
        self.assertEqual(agent.calls, ["1", "2"])
        self.assertEqual([m["text"] for m in sender.sent], ["收到。"])

    def test_different_groups_run_in_parallel(self):
        agent, sender = FakeAgent(), FakeSender()
        service = self.service(
            agent=agent,
            sender=sender,
            policy=self.policy(allowed_groups=frozenset({"42", "43"})),
        )

        async def run():
            agent.hold = asyncio.Event()
            first = asyncio.create_task(service.handle_message(event("1")))
            second = asyncio.create_task(service.handle_message(event("2", group="43")))
            await wait_for(lambda: agent.active == 2)
            agent.hold.set()
            await asyncio.gather(first, second)

        asyncio.run(run())
        self.assertEqual(agent.max_active, 2)
        self.assertEqual(len(sender.sent), 2)

    def test_a_failed_model_call_does_not_send_a_canned_reply(self):
        agent, sender = FakeAgent(), FakeSender()
        agent.fail = True
        service = self.service(agent=agent, sender=sender)
        with self.assertLogs("qunbot.runtime.service", level="ERROR"):
            asyncio.run(service.handle_message(event("1")))
        self.assertEqual(sender.sent, [])
        self.assertEqual(self.store.message_count("group:42"), 1)

    def test_a_duplicate_frame_does_not_reply_twice(self):
        agent, sender, affection = FakeAgent(), FakeSender(), RecordingObserver()
        service = self.service(
            agent=agent,
            sender=sender,
            affection=affection,
            policy=self.policy(affection_auto_enabled=True),
        )

        async def run():
            await service.handle_message(event("1"))
            await service.handle_message(event("1"))  # redelivered frame
            await service.aclose()

        asyncio.run(run())
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(agent.calls, ["1"])
        self.assertEqual(affection.seen, ["1"])

    # --- post-reply observations -----------------------------------------

    def test_an_observation_is_admitted_at_most_once_per_event_id(self):
        observer = RecordingObserver()
        service = self.service(observers=(observer,))

        async def run():
            self.assertTrue(
                service.submit_observation("observer:0:1", observer, event("1"), "hi")
            )
            self.assertFalse(
                service.submit_observation("observer:0:1", observer, event("1"), "hi")
            )
            await service.aclose()

        asyncio.run(run())
        self.assertEqual(observer.seen, ["1"])
        self.assertEqual(observer.started, 1)
        self.assertEqual(service.observations.deduplicated, 1)

    def test_a_failed_observation_is_logged_and_never_replayed(self):
        observer = RecordingObserver(fail=True)
        service = self.service(observers=(observer,))

        async def run():
            service.submit_observation("observer:0:1", observer, event("1"), "hi")
            await service.aclose()
            # A failure replay must not observe the same turn a second time.
            self.assertFalse(
                service.submit_observation("observer:0:1", observer, event("1"), "hi")
            )
            await service.aclose()

        with self.assertLogs("qunbot.runtime.service", level="ERROR"):
            asyncio.run(run())
        self.assertEqual(observer.started, 1)
        self.assertEqual(service.observations.failed, 1)
        self.assertEqual(service.observations.completed, 0)

    def test_the_observation_backlog_is_bounded(self):
        observer = RecordingObserver(delay=0.05)
        service = self.service(observers=(observer,), observation_backlog=1)

        async def run():
            for index in range(10):
                service.submit_observation(
                    f"observer:0:{index}", observer, event(str(index)), "hi"
                )
            self.assertLessEqual(service.pending_observations, 1)
            await service.aclose()

        with self.assertLogs("qunbot.runtime.service", level="WARNING"):
            asyncio.run(run())
        self.assertEqual(service.observations.enqueued, 1)
        self.assertEqual(service.observations.dropped, 9)
        self.assertEqual(observer.started, 1)

    def test_aclose_drains_queued_observations(self):
        observer = RecordingObserver(delay=0.01)
        service = self.service(observers=(observer,))

        async def run():
            for index in range(3):
                service.submit_observation(
                    f"observer:0:{index}", observer, event(str(index)), "hi"
                )
            await service.aclose()

        asyncio.run(run())
        self.assertEqual(sorted(observer.seen), ["0", "1", "2"])
        self.assertEqual(service.observations.completed, 3)

    def test_aclose_gives_up_on_a_stuck_observation(self):
        observer = RecordingObserver(hang=True)
        service = self.service(observers=(observer,))

        async def run():
            service.submit_observation("observer:0:1", observer, event("1"), "hi")
            started = asyncio.get_running_loop().time()
            await service.aclose(timeout=0.05)
            return asyncio.get_running_loop().time() - started

        with self.assertLogs("qunbot.runtime.service", level="WARNING"):
            elapsed = asyncio.run(run())
        self.assertLess(elapsed, 1.0)
        self.assertEqual(observer.started, 1)
        self.assertEqual(service.observations.completed, 0)

    def test_after_aclose_new_observations_are_refused(self):
        observer = RecordingObserver()
        service = self.service(observers=(observer,))

        async def run():
            await service.aclose()
            return service.submit_observation(
                "observer:0:1", observer, event("1"), "hi"
            )

        self.assertFalse(asyncio.run(run()))

    # --- restart behaviour ------------------------------------------------

    def test_last_reply_is_restored_from_history(self):
        self.store.add_message("m1", "group:42", "7", "小明", "user", "在吗")
        self.store.add_message("reply:m1", "group:42", "bot", "Bot", "assistant", "在")
        service = self.service()
        self.assertGreater(service.last_reply["group:42"], 0)
        self.assertLessEqual(service.last_reply["group:42"], time.time())

    def test_last_reply_is_empty_when_the_bot_never_spoke(self):
        self.store.add_message("m1", "group:42", "7", "小明", "user", "在吗")
        service = self.service()
        self.assertEqual(service.last_reply, {})

    def test_restoring_can_be_switched_off(self):
        self.store.add_message("reply:m1", "group:42", "bot", "Bot", "assistant", "在")
        service = self.service(restore_last_reply=False)
        self.assertEqual(service.last_reply, {})


if __name__ == "__main__":
    unittest.main()
