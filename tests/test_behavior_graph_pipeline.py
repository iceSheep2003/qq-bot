from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_behavior_graph import evaluate
from scripts.compile_behavior_graph import expression_family, situation_family


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


class BehaviorGraphEvaluationTests(unittest.TestCase):
    def test_situation_family_keeps_raw_case_detail_out_of_index_key(self):
        key, label = situation_family(
            "群里有人用夸张说法开玩笑，随后转到别的话题", [], "banter"
        )
        self.assertEqual(key, "banter_or_joke")
        self.assertIn("玩笑", label)

    def test_expression_family_keeps_reusable_structure(self):
        key, label = expression_family("先肯定对方，再补充自己的理解", "agree", "agree")
        self.assertEqual(key, "agree_then_add")
        self.assertIn("补充", label)

    def test_mechanical_question_pattern_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            corpus = root / "corpus"
            output = root / "output"
            source.mkdir()
            corpus.mkdir()
            proposal = {
                "case_id": "case-1",
                "source_message_ids": ["m1", "m2"],
                "required_message_ids": ["m1", "m2"],
                "strategy": "tease_gently",
                "dialogue_act": "tease",
                "media_intent": "none",
                "situation": "有人开玩笑",
                "expression_pattern": "用一连串具体问题追问对方",
                "avoid": [], "topic_terms": [], "abstraction": "轻松接梗",
            }
            write_jsonl(source / "behavior_proposals.jsonl", [proposal])
            write_jsonl(corpus / "messages.jsonl", [{"id": "m1"}, {"id": "m2"}])
            write_jsonl(corpus / "training_candidates.jsonl", [{
                "id": "case-1", "split": "train", "reply_target_id": "m1", "continuation_id": "m2"
            }])
            report = evaluate(source, corpus, output, min_support=1)
            self.assertEqual(report["reasons"]["unsafe_question_pattern"], 1)

    def test_evaluation_requires_reply_evidence_and_quarantines_pii(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            corpus = root / "corpus"
            output = root / "output"
            source.mkdir()
            corpus.mkdir()
            messages = [
                {"id": "m1"},
                {"id": "m2"},
                {"id": "m3"},
            ]
            candidates = [
                {
                    "id": "case-1",
                    "split": "train",
                    "reply_target_id": "m1",
                    "continuation_id": "m2",
                },
                {
                    "id": "case-2",
                    "split": "train",
                    "reply_target_id": "m1",
                    "continuation_id": "m3",
                },
            ]
            proposal = {
                "case_id": "case-1",
                "source_message_ids": ["m1", "m2"],
                "required_message_ids": ["m1", "m2"],
                "strategy": "agree",
                "dialogue_act": "agree",
                "media_intent": "none",
                "situation": "有人表达观点，另一人简短附和",
                "expression_pattern": "先肯定，再补充一句",
                "avoid": [],
                "topic_terms": [],
                "abstraction": "用短句接住已经形成的共识",
            }
            sensitive = {**proposal, "case_id": "case-2", "situation": "QQ号 12345678"}
            write_jsonl(source / "behavior_proposals.jsonl", [proposal, sensitive])
            write_jsonl(corpus / "messages.jsonl", messages)
            write_jsonl(corpus / "training_candidates.jsonl", candidates)
            report = evaluate(source, corpus, output, min_support=1)
            self.assertEqual(report["valid_proposals"], 1)
            self.assertEqual(report["quarantined_proposals"], 1)
            with (output / "published_options.jsonl").open() as stream:
                published = [json.loads(line) for line in stream]
            self.assertEqual(published[0]["strategy"], "agree")


if __name__ == "__main__":
    unittest.main()
