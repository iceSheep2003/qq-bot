import tempfile
import time
import unittest
from pathlib import Path

from qunbot.conversation.projection import ConversationProjector
from qunbot.conversation_compaction.service import ConversationCompactor
from qunbot.domain import MessageEvent
from qunbot.replies.dispatcher import ReplyDispatcher
from qunbot.replies.models import ReplyPlan
from qunbot.replies.segmentation import TextSegmenter
from qunbot.storage.conversation import ConversationStore
from qunbot.storage.database import SqliteDatabase
from qunbot.storage.observations import ObservationOutbox
from qunbot.storage.ontology import GraphStore
from qunbot.storage.prompt_sessions import PromptSessionStore


class LedgerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(Path(self.tmp.name) / "test.db")
        self.store = ConversationStore(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_partial_send_records_success_and_unknown_without_resending(self):
        class Sender:
            def __init__(self):
                self.calls = []

            async def send(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 2:
                    raise TimeoutError()
                return {"message_id": "111"}

        sender = Sender()
        dispatcher = ReplyDispatcher(sender, segmenter=TextSegmenter(4))

        async def intent(index, kind, text):
            return self.store.begin_part("reply:test", index, "group:42", kind, text)

        async def receipt(part):
            self.store.settle_part("reply:test", part)
            if part.status == "sent":
                self.store.add_message(
                    f"part:{part.index}",
                    "group:42",
                    "bot",
                    "Bot",
                    "assistant",
                    part.transcript,
                )

        plan = ReplyPlan("第一段。第二段。第三段。", ("text",))
        with self.assertRaises(TimeoutError):
            await dispatcher.dispatch(
                plan, group_id="42", user_id=None, on_intent=intent, on_receipt=receipt
            )
        self.assertEqual(
            [r["content"] for r in self.store.actual_recent("group:42")], ["第一段。"]
        )
        with self.assertRaises(RuntimeError):
            await dispatcher.dispatch(
                plan, group_id="42", user_id=None, on_intent=intent, on_receipt=receipt
            )
        self.assertEqual(len(sender.calls), 2)

    async def test_event_time_and_projection_survive_restart_without_duplicates(self):
        event = MessageEvent(
            "e1",
            "group:42",
            "42",
            "7",
            "小明",
            "你好",
            (),
            False,
            (),
            int(time.time()) - 30,
        )
        self.assertTrue(self.store.add_event(event))
        self.assertFalse(self.store.add_event(event))
        self.assertEqual(
            self.store.actual_recent(event.scope)[0]["event_time"],
            event.timestamp * 1000,
        )
        graph = GraphStore(self.db)
        projector = ConversationProjector(self.store, graph)
        self.assertEqual(projector.process(), 1)
        self.assertEqual(projector.process(), 0)
        self.assertEqual(len(graph.list_nodes(event.scope, "Message")), 1)
        self.assertEqual(graph.stats()["foreign_key_violations"], 0)

    async def test_compaction_triggers_from_unanswered_actual_messages(self):
        class Model:
            def __init__(self):
                self.requests = []

            async def complete(self, messages, **kwargs):
                self.requests.append(messages)
                return {
                    "choices": [
                        {
                            "message": {
                                "content": '{"scene_summary":"继续聊模型","bot_continuity":{"recent_statements":[]}}'
                            }
                        }
                    ]
                }

        for index in range(9):
            self.store.add_message(
                f"m{index}",
                "group:42",
                "7",
                "小明",
                "user",
                f"synthetic message {index}",
            )
        model = Model()
        compactor = ConversationCompactor(
            model,
            PromptSessionStore(self.db),
            self.store,
            max_messages=8,
            tail_messages=3,
        )
        self.assertTrue(await compactor.compact_if_needed("group:42"))
        self.assertEqual(self.store.message_count("group:42"), 9)
        self.assertEqual(self.store.continuity_snapshot("group:42")["covered_id"], 6)
        self.assertFalse(await compactor.compact_if_needed("group:42"))
        self.assertEqual(len(model.requests), 1)

    async def test_expired_continuity_snapshot_is_not_recalled(self):
        self.db.db.execute(
            "INSERT INTO conversation_snapshots(scope,covered_id,last_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?)",
            ("group:42", 6, 6, '{"role":"user","content":"旧现场"}', 1),
        )
        self.assertIsNone(
            self.store.continuity_snapshot("group:42", max_age_seconds=60)
        )

    async def test_compaction_failure_backs_off_instead_of_retrying_every_turn(self):
        class BrokenModel:
            def __init__(self):
                self.calls = 0

            async def complete(self, messages, **kwargs):
                self.calls += 1
                raise TimeoutError("provider unavailable")

        for index in range(9):
            self.store.add_message(
                f"failed:{index}", "group:42", "7", "小明", "user", f"热聊 {index}"
            )
        model = BrokenModel()
        compactor = ConversationCompactor(
            model, PromptSessionStore(self.db), self.store,
            max_messages=8, tail_messages=3,
        )
        self.assertFalse(await compactor.compact_if_needed("group:42"))
        self.assertFalse(await compactor.compact_if_needed("group:42"))
        self.assertEqual(model.calls, 1)

    async def test_durable_advisory_recovery_distinguishes_idempotent_consumers(self):
        jobs = ObservationOutbox(self.db)
        self.assertEqual(
            jobs.enqueue("a", "mood", {}, "", None, replayable=True), "queued"
        )
        self.assertEqual(
            jobs.enqueue("a", "mood", {}, "", None, replayable=True), "duplicate"
        )
        jobs.enqueue("b", "style", {}, "", None, replayable=False)
        jobs.lease(("mood",))
        jobs.lease(("style",))
        jobs.recover()
        self.assertEqual(jobs.lease(("mood",))["id"], "a")
        self.assertIsNone(jobs.lease(("style",)))
