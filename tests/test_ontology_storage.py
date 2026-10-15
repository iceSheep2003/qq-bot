import tempfile
import unittest
from pathlib import Path

from qunbot.contracts.schema import ContractError
from qunbot.knowledge.service import KnowledgeService
from qunbot.ontology.objects import entity, edge, ref
from qunbot.storage.database import SqliteDatabase
from qunbot.storage.ontology import GraphStore


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(Path(self.temp.name) / "test.db")
        self.graph = GraphStore(self.db)
        self.knowledge = KnowledgeService(self.graph)
        self.person = entity(
            "Person", "group:1", "person:1", 1, platform="qq", account_ref="account:1"
        )
        self.message = entity(
            "Message",
            "group:1",
            "message:1",
            1,
            platform_message_id="1",
            author_ref=ref(self.person),
            event_time=1,
            recorded_at=1,
            direction="inbound",
            origin="member",
            parts=[{"kind": "text", "text": "我喜欢咖啡"}],
            reply_to_ref=None,
            delivery_status="received",
        )
        self.predicate = entity(
            "Concept",
            "group:1",
            "predicate:likes",
            1,
            name="喜欢",
            aliases=["person"],
            kind="predicate",
            definition="本人表达的偏好",
            status="accepted",
            subject_types=["Person"],
            object_types=["Concept"],
            value_kinds=["text"],
        )
        self.graph.write(
            "group:1",
            [self.person, self.message],
            [edge("authored", self.person, self.message, 1)],
            reason="test",
        )

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def report(self):
        return self.knowledge.add_report(
            "group:1",
            message_id="message:1",
            subject_id="person:1",
            predicate=self.predicate,
            start=0,
            end=5,
            value="咖啡",
            source_kind="self_report",
            accepted=True,
        )

    def test_idempotence_and_evidence_search(self):
        identity = self.report()
        self.assertEqual(identity, self.report())
        self.assertEqual(self.graph.revision("group:1", identity), 1)
        hits = self.knowledge.search("group:1", "咖啡")
        self.assertEqual(len(hits), 1)
        self.assertEqual(
            self.knowledge.evidence("group:1", hits[0]["evidence_ids"])[0]["excerpt"],
            "我喜欢咖啡",
        )
        self.assertEqual(self.knowledge.search("group:2", "咖啡"), [])

    def test_revision_cas_and_withdrawn_not_revived(self):
        identity = self.report()
        self.knowledge.revise(
            "group:1", identity, "withdrawn", expected_revision=1, reason="withdrawal"
        )
        self.assertEqual(self.knowledge.search("group:1", "咖啡"), [])
        self.report()
        self.assertEqual(
            self.graph.get("group:1", identity)["epistemic_status"], "withdrawn"
        )
        with self.assertRaises(ContractError):
            self.knowledge.revise(
                "group:1", identity, "disputed", expected_revision=1, reason="stale"
            )

    def test_mutating_source_checks_dependants_and_rolls_back(self):
        self.report()
        self.message["parts"] = [{"kind": "text", "text": "不存在"}]
        with self.assertRaises(ContractError):
            self.graph.write("group:1", [self.message], [], reason="tamper")
        self.assertEqual(
            self.graph.get("group:1", "message:1")["parts"][0]["text"], "我喜欢咖啡"
        )

    def test_no_missing_or_cross_scope_reference(self):
        self.message["author_ref"]["scope"] = "group:2"
        with self.assertRaises(ContractError):
            self.graph.write("group:1", [self.message], [], reason="cross-scope")

    def test_candidate_not_default_recall(self):
        self.knowledge.add_report(
            "group:1",
            message_id="message:1",
            subject_id="person:1",
            predicate=self.predicate,
            start=0,
            end=5,
            value="咖啡",
        )
        self.assertEqual(self.knowledge.search("group:1", "咖啡"), [])
        self.assertEqual(
            len(self.knowledge.search("group:1", "咖啡", include_candidates=True)), 1
        )
