from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from tools.experiments.evaluate_behavior_graph import evaluate
from tools.experiments.compile_behavior_graph import compile_graph, expression_family, situation_family


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


class BehaviorGraphEvaluationTests(unittest.TestCase):
    def test_situation_family_keeps_raw_case_detail_out_of_index_key(self):
        key, label = situation_family(
            "群里有人用夸张说法开玩笑，随后转到别的话题", []
        )
        self.assertEqual(key, "banter_or_joke")
        self.assertIn("玩笑", label)

    def test_situation_family_is_derived_from_text_alone(self):
        """The family must not be able to read the case's own label.

        It used to fall back to the strategy when no keyword matched, which
        filed 15% of cases under a family *because* of the label that family
        was then used to predict. Any "does the situation predict the strategy?"
        statistic was self-fulfilling for those cases, inflating the measured
        ceiling by 1.7pt (13 classes) / 3.0pt (9 classes).

        Asserting on the signature is the point: while a strategy parameter
        exists, the shortcut can come back.
        """
        self.assertEqual(
            list(inspect.signature(situation_family).parameters), ["text", "cues"]
        )
        # No marker matches -> the neutral bucket, not a label-derived guess.
        self.assertEqual(
            situation_family("群友随口聊了两句今天天气", [])[0], "general_group_exchange"
        )
        # A marker still wins, and it comes from the text.
        self.assertEqual(situation_family("有人问复习资料在哪找", [])[0], "direct_question")

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


class CompiledNodeShapeTests(unittest.TestCase):
    """The compiled graph must carry the fields the retriever actually reads.

    This is the test that was missing, and its absence is why two regressions
    lived in the compiler unnoticed: nothing rebuilt the graph from proposals,
    and the shipped snapshot predated both. ExpressionPattern nodes lost
    ``name``/``raw_variants`` (they were keyed by raw wording instead of by
    family, so the per-identity accumulator never fired and recompiling produced
    2004 nodes against the snapshot's 15), and InteractionCase nodes lost
    ``expression_pattern_text``/``expression_family``. The result was a graph
    the retriever could only partly read.
    """

    def _proposal(self, case_id: str, expression: str, avoid: list[str]) -> dict:
        return {
            "case_id": case_id,
            "source_message_ids": ["m1", "m2"],
            "required_message_ids": ["m1", "m2"],
            "strategy": "agree",
            "dialogue_act": "agree",
            "media_intent": "none",
            "situation": "有人表达观点，另一人简短附和",
            "expression_pattern": expression,
            "avoid": avoid,
            "topic_terms": [],
            "abstraction": "用短句接住已经形成的共识",
            "outcome": "resolved",
        }

    def _compile(self, proposals: list[dict]) -> list[dict]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / "source"
        source.mkdir()
        write_jsonl(source / "behavior_proposals.jsonl", proposals)
        (source / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "proposals": len(proposals)}),
            encoding="utf-8",
        )
        output = root / "compiled"
        compile_graph(source, output)
        return [
            json.loads(line) for line in (output / "nodes.jsonl").open(encoding="utf-8")
        ]

    def test_cases_sharing_an_expression_family_share_one_node(self):
        """Keying by raw wording builds one node per case and the accumulator —
        which is what makes ``avoid``/``raw_variants`` worth reading — never
        fires."""
        nodes = self._compile(
            [
                self._proposal("case-1", "先肯定对方，再补充一句", ["过度承诺"]),
                self._proposal("case-2", "先认同，再补一句", ["直接否定"]),
            ]
        )
        patterns = [n for n in nodes if n["type"] == "ExpressionPattern"]
        self.assertEqual(len(patterns), 1)
        node = patterns[0]
        for field in ("family_key", "name", "raw_variants", "avoid"):
            self.assertIn(field, node)
        self.assertEqual(node["family_key"], "agree_then_add")
        self.assertEqual(node["name"], "先认同再补充")
        self.assertEqual(
            set(node["raw_variants"]), {"先肯定对方，再补充一句", "先认同，再补一句"}
        )
        self.assertEqual(set(node["avoid"]), {"过度承诺", "直接否定"})

    def test_case_nodes_carry_the_case_level_text(self):
        nodes = self._compile(
            [self._proposal("case-1", "先肯定对方，再补充一句", [])]
        )
        case = next(n for n in nodes if n["type"] == "InteractionCase")
        self.assertEqual(case["expression_pattern_text"], "先肯定对方，再补充一句")
        self.assertEqual(case["expression_family"], "agree_then_add")


if __name__ == "__main__":
    unittest.main()
