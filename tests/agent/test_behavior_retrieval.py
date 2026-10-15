from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qunbot.agent.cognition.experience.retrieval import BehaviorRetriever
from qunbot.agent.cognition.experience.graph_retrieval import BehaviorGraphRetriever
from qunbot.agent.cognition.experience.runtime import CompiledBehaviorExperience


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


class BehaviorRetrieverTests(unittest.TestCase):
    def test_graph_intent_bonus_prioritizes_direct_answer_and_deescalation(self):
        answer = BehaviorGraphRetriever._intent_bonus({"资料", "怎么"})
        calm = BehaviorGraphRetriever._intent_bonus({"争论", "缓和"})
        self.assertGreater(answer["answer_directly"], answer["supplement"])
        self.assertGreater(calm["deescalate"], calm.get("supplement", 0))

    def test_returns_bounded_options_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text(
                json.dumps({"pipeline_version": "test-v1"}), encoding="utf-8"
            )
            (root / "published_options.jsonl").write_text(
                json.dumps(
                    {
                        "strategy": "banter",
                        "support_case_count": 5,
                        "representatives": [
                            {
                                "dialogue_act": "joke",
                                "situation": "有人调侃并使用群内梗",
                                "expression_pattern": "用轻松的夸张说法接话",
                                "avoid": ["不要认真说教"],
                                "media_intent": "meme",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            options = BehaviorRetriever(root).retrieve("群里有人开玩笑调侃", limit=9)
            self.assertEqual(len(options), 1)
            self.assertEqual(options[0].strategy, "banter")
            self.assertEqual(options[0].media_intent, "meme")
            self.assertLessEqual(len(options), 3)

    def test_empty_query_does_not_return_a_default_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "manifest.json").write_text("{}", encoding="utf-8")
            (root / "published_options.jsonl").write_text("", encoding="utf-8")
            self.assertEqual(BehaviorRetriever(root).retrieve(""), [])

    def test_graph_retriever_requires_one_complete_evidence_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = [
                {"id": "case", "type": "InteractionCase", "case_id": "c1", "situation_text": "有人调侃开玩笑", "observable_cues": ["有人开玩笑调侃", "有人用夸张说法起哄"], "topic_terms": []},
                {"id": "strategy", "type": "Strategy", "key": "banter", "support_case_count": 2},
                {"id": "action", "type": "DialogueAct", "key": "joke"},
                {"id": "expr", "type": "ExpressionPattern", "name": "夸张接梗", "raw_variants": ["用夸张说法接话"], "avoid": []},
                {"id": "media", "type": "MediaIntent", "key": "meme"},
                {"id": "situation", "type": "Situation", "family_key": "banter_or_joke", "name": "玩笑、调侃或群内梗"},
                {"id": "evidence", "type": "Evidence"},
            ]
            edges = []
            for relation, source, target in (
                ("has_evidence", "case", "evidence"),
                ("supports", "case", "strategy"),
                ("realizes_action", "case", "action"),
                ("expressed_as", "case", "expr"),
                ("may_use", "case", "media"),
                ("observed_in", "case", "situation"),
            ):
                edges.append({"relation": relation, "source": source, "target": target, "evidence_id": "evidence"})
            (root / "manifest.json").write_text(json.dumps({"pipeline_version": "test-v1"}), encoding="utf-8")
            write_jsonl(root / "nodes.jsonl", nodes)
            write_jsonl(root / "edges.jsonl", edges)
            options = BehaviorGraphRetriever(root).retrieve("群里有人开玩笑")
            self.assertEqual(options[0].strategy, "玩梗")
            self.assertEqual(options[0].examples[0]["evidence_id"], "evidence")

    def test_runtime_adapter_returns_bounded_read_only_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = [
                {"id": "case", "type": "InteractionCase", "case_id": "c1", "situation_text": "有人调侃开玩笑", "observable_cues": ["有人开玩笑调侃", "有人用夸张说法起哄"], "topic_terms": []},
                {"id": "strategy", "type": "Strategy", "key": "banter", "support_case_count": 2},
                {"id": "action", "type": "DialogueAct", "key": "joke"},
                {"id": "expr", "type": "ExpressionPattern", "name": "夸张接梗", "raw_variants": ["用轻松的夸张说法接话"], "avoid": ["不要照抄案例"]},
                {"id": "media", "type": "MediaIntent", "key": "meme"},
                {"id": "situation", "type": "Situation", "family_key": "banter_or_joke", "name": "玩笑、调侃或群内梗"},
                {"id": "evidence", "type": "Evidence"},
            ]
            edges = []
            for relation, source, target in (
                ("has_evidence", "case", "evidence"),
                ("supports", "case", "strategy"),
                ("realizes_action", "case", "action"),
                ("expressed_as", "case", "expr"),
                ("may_use", "case", "media"),
                ("observed_in", "case", "situation"),
            ):
                edges.append({"relation": relation, "source": source, "target": target, "evidence_id": "evidence"})
            (root / "manifest.json").write_text(json.dumps({"pipeline_version": "test-v1"}), encoding="utf-8")
            write_jsonl(root / "nodes.jsonl", nodes)
            write_jsonl(root / "edges.jsonl", edges)
            provider = CompiledBehaviorExperience(BehaviorGraphRetriever(root))
            context = provider.context_for("群里有人开玩笑调侃")
            self.assertIn("行为经验候选", context)
            self.assertIn("玩梗", context)
            self.assertLessEqual(len(context), 1200)


if __name__ == "__main__":
    unittest.main()
