import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qqbot.interfaces.console.ontology import OntologyConsole
from qqbot.agent.conversation.projection import ConversationProjector
from qqbot.contracts.models import MessageEvent
from qqbot.interfaces.webui.memory_query import MemoryConsoleQuery
from qqbot.agent.memory.knowledge.coordinator import OntologyMemory
from qqbot.agent.memory.knowledge.service import KnowledgeService
from qqbot.contracts.ontology.objects import entity, stable_id
from qqbot.infrastructure.storage.conversation import ConversationStore
from qqbot.infrastructure.storage.database import SqliteDatabase
from qqbot.infrastructure.storage.extraction_runs import ExtractionRunStore
from qqbot.infrastructure.storage.ontology import GraphStore


class OntologyConsoleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(Path(self.tmp.name) / "test.db")
        self.graph = GraphStore(self.db)
        self.ledger = ConversationStore(self.db)
        self.knowledge = KnowledgeService(self.graph)
        self.console = OntologyConsole(self.knowledge)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def topic(self, identity, scope="group:1", status="candidate"):
        return entity(
            "Concept",
            scope,
            identity,
            1000,
            name=identity,
            aliases=[],
            kind="topic",
            definition="有来源的候选话题",
            status=status,
            subject_types=[],
            object_types=[],
            value_kinds=[],
        )

    def test_paging_search_before_limit_and_scope_isolation(self):
        topics = [self.topic(f"topic:{i:03}") for i in range(205)]
        self.graph.write("group:1", topics, [], reason="test")
        self.graph.write(
            "group:2", [self.topic("topic:other", "group:2")], [], reason="test"
        )
        first = self.console.records(
            "Concept", "group:1", limit=200, concept_kind="topic"
        )
        second = self.console.records(
            "Concept",
            "group:1",
            limit=200,
            after=first[-1]["cursor"],
            concept_kind="topic",
        )
        self.assertEqual((len(first), len(second)), (200, 5))
        self.assertEqual(len({r["id"] for r in first + second}), 205)
        self.assertEqual(
            self.console.records("Concept", "group:1", query="204", limit=1)[0][
                "label"
            ],
            "topic:204",
        )
        self.assertEqual(
            self.console.records("Concept", "group:1", status="accepted"), []
        )
        self.assertEqual(self.console.overview("group:1")["memories"]["topics"], 205)
        self.assertEqual(self.console.detail("group:2", "topic:000"), None)
        with self.assertRaises(ValueError):
            self.console.records("Concept", after="[1,2]")

    def seed_report(self, value="咖啡", accepted=False):
        event = MessageEvent(
            "test:1",
            "group:1",
            "1",
            "7",
            "甲",
            "我喜欢咖啡",
            (),
            False,
            (),
            1,
            platform_message_id="101",
        )
        self.ledger.add_event(event)
        ConversationProjector(self.ledger, self.graph).process()
        predicate = entity(
            "Concept",
            event.scope,
            "predicate:likes",
            1000,
            name="偏好",
            aliases=[],
            kind="predicate",
            definition="表达的偏好",
            status="accepted",
            subject_types=["Person"],
            object_types=["Concept"],
            value_kinds=["text"],
        )
        return self.knowledge.add_report(
            event.scope,
            message_id=stable_id("message", event.scope, event.event_id),
            subject_id=stable_id("person", event.scope, event.user_id),
            predicate=predicate,
            start=0,
            end=5,
            value=value,
            accepted=accepted,
            source_kind="self_report",
        )

    def test_claim_labels_evidence_and_history_have_no_fabricated_confidence(self):
        identity = self.seed_report(accepted=True)
        record = self.console.records("Claim", "group:1")[0]
        self.assertEqual(record["status"], "accepted_as_report")
        self.assertNotIn("confidence", record)
        self.knowledge.revise(
            "group:1",
            identity,
            "disputed",
            expected_revision=1,
            reason="test correction",
        )
        detail = self.console.detail("group:1", identity)
        self.assertEqual(detail["evidence"][0]["excerpt"], "我喜欢咖啡")
        self.assertEqual([r["revision"] for r in detail["revisions"]], [2, 1])
        self.assertEqual(detail["revision"], 2)

    def test_graph_keeps_connected_people_when_many_claims_exist(self):
        for index in range(70):
            self.seed_report(str(index))
        view = self.console.graph_view("group:1", 20)
        self.assertLessEqual(len(view["nodes"]), 20)
        self.assertTrue(any(n["entity_type"] == "Person" for n in view["nodes"]))
        self.assertTrue(any(l["kind"] == "about" for l in view["links"]))
        identities = {n["id"] for n in view["nodes"]}
        self.assertTrue(
            all(
                l["source"] in identities and l["target"] in identities
                for l in view["links"]
            )
        )

    def test_new_console_never_reads_legacy_lists(self):
        self.seed_report()

        def forbidden(*args, **kwargs):
            raise AssertionError("legacy archive was consulted")

        memory = SimpleNamespace(
            ontology=self.knowledge,
            memories=SimpleNamespace(list_memories=forbidden),
            overview=forbidden,
            topics=forbidden,
            extraction_runs=lambda *a: [],
        )
        query = MemoryConsoleQuery(
            SimpleNamespace(
                agent=SimpleNamespace(memories=memory),
                people=None,
                conversations=self.ledger,
            )
        )
        self.assertEqual(query.overview("group:1")["source"], "ontology")
        self.assertEqual(query.memories("group:1")[0]["status"], "candidate")
        self.assertEqual(query.topics("group:1"), [])
        self.assertEqual(query.slang("group:1"), [])
        self.assertEqual(len(query.people_list("group:1")), 1)

    async def test_live_extraction_audit_and_failed_cursor_are_not_old_logs(self):
        class Extractor:
            VERSION = "test-v1"
            fail = False

            async def extract(self, scope, messages):
                if self.fail:
                    raise ConnectionError("private provider error")
                return {"proposed": 2, "written": 1, "rejected": 1}

        self.seed_report()
        extractor = Extractor()
        runs = ExtractionRunStore(self.db)
        memory = OntologyMemory(
            self.knowledge,
            extractor,
            ConversationProjector(self.ledger, self.graph),
            self.ledger,
            SimpleNamespace(memories=None),
            runs=runs,
        )
        await memory.extract("group:1")
        result = memory.extraction_runs("group:1")[0]
        self.assertEqual((result["written"], result["rejected"]), (1, 1))
        self.assertEqual(self.ledger.knowledge_cursor("group:1"), 1)
        self.ledger.add_event(
            MessageEvent(
                "test:2", "group:1", "1", "7", "甲", "还在聊", (), False, (), 2
            )
        )
        extractor.fail = True
        with self.assertRaises(ConnectionError):
            await memory.extract("group:1")
        self.assertEqual(self.ledger.knowledge_cursor("group:1"), 1)
        failed = next(r for r in runs.list_runs() if r["status"] == "failed")
        self.assertNotIn("private", failed["error"])
        runs.begin("group:2", [{"id": 3}], "test")
        runs.recover()
        self.assertEqual(runs.list_runs("group:2")[0]["status"], "unknown")

    def test_compaction_does_not_skip_old_uncovered_messages(self):
        for index in range(300):
            self.ledger.add_message(
                f"m:{index}", "group:1", "7", "甲", "user", "合成热聊"
            )
        candidate = self.ledger.ledger_compaction_candidate(
            "group:1", max_messages=100, max_chars=20000, tail_messages=20
        )
        self.assertEqual(candidate["rows"][0]["id"], 1)
        self.assertEqual(candidate["covered_id"], 220)
        self.ledger.commit_continuity(
            "group:1", candidate, {"role": "user", "content": "summary"}
        )
        self.ledger.add_message("m:new", "group:1", "7", "甲", "user", "新的热聊")
        next_candidate = self.ledger.ledger_compaction_candidate(
            "group:1", max_messages=40, max_chars=20000, tail_messages=20
        )
        self.assertEqual(next_candidate["rows"][0]["id"], 221)
