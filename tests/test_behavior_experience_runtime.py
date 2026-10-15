"""behavior experience: the optional compiled graph as a reply-time read model.

The graph is kernel, not an extension: ``app.py`` hands
``configured_behavior_experience()`` to the Agent, and ``Agent.build_messages``
renders its hint into the *dynamic suffix* under ``行为经验``. These tests pin
the three properties that make that safe:

*it is optional* — no snapshot configured, or a broken one, disables the read
model without stopping the bot;

*it never touches the stable prefix* — enabling it must not change one byte of
the cached prefix, or every turn pays full price again;

*it tokenises each case once* — retrieval used to re-tokenise every case on
every call, which cost ~320 ms per turn on the event loop. That cost, not the
wiring, is why the graph shipped unconfigured.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from qunbot.domain import MessageEvent
from qunbot.experience import graph_retrieval
from qunbot.experience.graph_retrieval import BehaviorGraphRetriever
from qunbot.experience.retrieval import _terms
from qunbot.experience.runtime import (
    CompiledBehaviorExperience,
    configured_behavior_experience,
)
from qunbot.runtime.agent import Agent

_ENV_KEYS = (
    "BOT_BEHAVIOR_GRAPH_PATH",
    "BOT_BEHAVIOR_CALIBRATION_PATH",
    "BOT_BEHAVIOR_MAX_OPTIONS",
    "BOT_BEHAVIOR_MAX_CASES",
    "BOT_BEHAVIOR_CHAR_BUDGET",
)


@contextmanager
def env(**values):
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    for key, value in values.items():
        os.environ[key] = str(value)
    try:
        yield
    finally:
        for key in _ENV_KEYS:
            if saved[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved[key]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


#: Three cases with deliberately disjoint vocabulary, so which strategy wins a
#: query is not a matter of tie-breaking luck.
FIXTURE_CASES = [
    {
        "situation": "有人问考研复习资料在哪里找",
        "topics": ["资料", "复习"],
        "strategy": "answer_directly",
        "act": "answer",
        "expression": "回答并给出简短理由",
        "variants": ["先回答再补充一句理由"],
        "family": "direct_question",
        "support": 4,
    },
    {
        "situation": "有人用夸张说法开玩笑起哄",
        "topics": ["玩笑", "调侃"],
        "strategy": "banter",
        "act": "joke",
        "expression": "用夸张说法接梗",
        "variants": ["夸张地顺着接一句"],
        "family": "banter_or_joke",
        "support": 3,
    },
    {
        "situation": "有人抱怨压力大想放弃",
        "topics": ["压力", "放弃"],
        "strategy": "empathize",
        "act": "comfort",
        "expression": "先共情再鼓励",
        "variants": ["先说理解他的难处"],
        "family": "emotional_complaint",
        "support": 2,
    },
]


def write_graph(root: Path, cases: list[dict] | None = None) -> Path:
    """Write a structurally complete compiled snapshot.

    ``BehaviorGraphRetriever`` accepts nothing less than a full evidence path:
    case, strategy, action, expression, media, situation and evidence must all
    share one evidence id, so each fixture case gets its own complete chain.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    root.mkdir(parents=True, exist_ok=True)
    for index, case in enumerate(cases if cases is not None else FIXTURE_CASES):
        case_id = f"c{index}"
        evidence_id = f"ev{index}"
        nodes.append(
            {
                "id": case_id,
                "type": "InteractionCase",
                "case_id": case_id,
                "situation_text": case["situation"],
                "observable_cues": case.get("cues", []),
                "topic_terms": case.get("topics", []),
            }
        )
        nodes.append({"id": evidence_id, "type": "Evidence"})
        chain = {
            "supports": {
                "id": f"st{index}",
                "type": "Strategy",
                "key": case["strategy"],
                "support_case_count": case.get("support", 1),
            },
            "realizes_action": {
                "id": f"ac{index}",
                "type": "DialogueAct",
                "key": case["act"],
            },
            "expressed_as": {
                "id": f"ex{index}",
                "type": "ExpressionPattern",
                "name": case["expression"],
                "raw_variants": case.get("variants", []),
                "avoid": case.get("avoid", []),
            },
            "may_use": {
                "id": f"md{index}",
                "type": "MediaIntent",
                "key": case.get("media", "none"),
            },
            "observed_in": {
                "id": f"si{index}",
                "type": "Situation",
                "family_key": case.get("family", "general_group_exchange"),
                "name": case["situation"],
            },
        }
        for relation, node in chain.items():
            nodes.append(node)
            edges.append(
                {
                    "relation": relation,
                    "source": case_id,
                    "target": node["id"],
                    "evidence_id": evidence_id,
                }
            )
        edges.append(
            {
                "relation": "has_evidence",
                "source": case_id,
                "target": evidence_id,
                "evidence_id": evidence_id,
            }
        )
    (root / "manifest.json").write_text(
        json.dumps({"pipeline_version": "fixture-v1"}), encoding="utf-8"
    )
    write_jsonl(root / "nodes.jsonl", nodes)
    write_jsonl(root / "edges.jsonl", edges)
    return root


def event(scope: str = "group:42", text: str = "在吗") -> MessageEvent:
    group = scope.split(":", 1)[1] if scope.startswith("group:") else None
    return MessageEvent("e1", scope, group, "7", "小明", text, (), True, (), 0)


# --------------------------------------------------------------------- config


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = write_graph(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_snapshot_configured_disables_the_read_model(self):
        with env():
            self.assertIsNone(configured_behavior_experience())

    def test_missing_snapshot_disables_the_read_model_without_raising(self):
        with env(BOT_BEHAVIOR_GRAPH_PATH=str(self.root / "does-not-exist")):
            self.assertIsNone(configured_behavior_experience())

    def test_incomplete_snapshot_disables_the_read_model_without_raising(self):
        incomplete = Path(self._tmp.name) / "incomplete"
        incomplete.mkdir()
        (incomplete / "manifest.json").write_text("{}", encoding="utf-8")
        with env(BOT_BEHAVIOR_GRAPH_PATH=str(incomplete)):
            self.assertIsNone(configured_behavior_experience())

    def test_configured_snapshot_loads_a_bounded_read_only_adapter(self):
        with env(BOT_BEHAVIOR_GRAPH_PATH=str(self.root)):
            experience = configured_behavior_experience()
        self.assertIsInstance(experience, CompiledBehaviorExperience)
        self.assertLessEqual(len(experience.context_for("资料 复习")), 1200)

    def test_unmatched_turn_contributes_nothing_rather_than_an_empty_block(self):
        with env(BOT_BEHAVIOR_GRAPH_PATH=str(self.root)):
            experience = configured_behavior_experience()
        self.assertEqual(experience.context_for("zzz 无关内容 qqq"), "")

    def test_blank_query_never_reaches_retrieval(self):
        with env(BOT_BEHAVIOR_GRAPH_PATH=str(self.root)):
            experience = configured_behavior_experience()
        self.assertEqual(experience.context_for("   "), "")


# ------------------------------------------------------------------ retrieval


class RetrievalTests(unittest.TestCase):
    """Semantics and cost of the indexed retrieval."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = write_graph(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_each_case_is_tokenised_once_at_load_not_once_per_query(self):
        """The whole point of the inverted index.

        Retrieval runs synchronously inside a turn. Tokenising every case on
        every call cost ~320 ms per turn, which is why the graph shipped with
        no configured snapshot. Loading must pay that cost once; a query must
        pay it only for itself.
        """
        calls: list[str] = []
        original = graph_retrieval._terms

        def counting(value):
            calls.append(value)
            return original(value)

        with mock.patch.object(graph_retrieval, "_terms", counting):
            retriever = BehaviorGraphRetriever(self.root)
            after_load = len(calls)
            retriever.retrieve("有人问复习资料", limit=3)
            after_query = len(calls)

        # Load tokenises the hint table plus every case path, and no fewer.
        self.assertGreaterEqual(after_load, len(FIXTURE_CASES))
        # A query tokenises itself and nothing else.
        self.assertEqual(after_query - after_load, 1)

    def test_index_is_built_once_and_reused_across_queries(self):
        retriever = BehaviorGraphRetriever(self.root)
        postings = retriever._postings
        retriever.retrieve("有人问复习资料")
        retriever.retrieve("有人开玩笑")
        self.assertIs(retriever._postings, postings)

    def test_direct_question_matches_the_answer_strategy(self):
        retriever = BehaviorGraphRetriever(self.root)
        options = retriever.retrieve("有人问复习资料在哪里找", limit=3)
        self.assertEqual(options[0].strategy, "answer_directly")
        self.assertEqual(options[0].dialogue_act, "answer")

    def test_joke_matches_the_banter_strategy(self):
        retriever = BehaviorGraphRetriever(self.root)
        options = retriever.retrieve("哈哈哈这个玩笑太夸张了", limit=3)
        self.assertEqual(options[0].strategy, "banter")

    def test_complaint_matches_the_empathize_strategy(self):
        retriever = BehaviorGraphRetriever(self.root)
        options = retriever.retrieve("最近压力好大想放弃", limit=3)
        self.assertEqual(options[0].strategy, "empathize")

    def test_indexed_scoring_matches_a_direct_scan(self):
        """The optimisation must not move a single score.

        This is the invariant the 65x speed-up was accepted under: the index
        only changes *when* a case is tokenised, never what it scores.
        """
        retriever = BehaviorGraphRetriever(self.root)
        for query in (
            "有人问复习资料在哪里找",
            "哈哈哈这个玩笑太夸张了",
            "最近压力好大想放弃",
            "复习 玩笑 压力 一起说",
        ):
            query_terms = _terms(query)
            intent = retriever._intent_bonus(query_terms)
            family = retriever.calibration.get(retriever._query_family(query), {})
            expected = {}
            for case, strategy, action, expression, media, situation, evidence in retriever.case_paths:
                searchable = retriever._searchable(
                    (case, strategy, action, expression, media, situation, evidence)
                )
                overlap = query_terms & _terms(searchable)
                overlap |= query_terms & retriever._hints.get(strategy.get("key", ""), set())
                if not overlap:
                    continue
                key = strategy.get("key", "")
                score = (
                    float(len(overlap))
                    + intent.get(key, 0.0)
                    + float(family.get(key, 0.0))
                )
                best = expected.get(strategy["id"])
                if best is None or score > best:
                    expected[strategy["id"]] = score

            produced = retriever.retrieve(query, limit=3)
            for option in produced:
                matched = [
                    score
                    for strategy_id, score in expected.items()
                    if retriever.nodes[strategy_id].get("key") == option.strategy
                ]
                self.assertTrue(matched, f"{option.strategy} scored nothing directly")
                # The reported score averages the best few rows of the strategy,
                # so it is never above the single best row the scan found.
                self.assertLessEqual(round(option.score, 9), round(max(matched), 9))


# ------------------------------------------------------------------ integration


class _Model:
    def __init__(self):
        self.requests: list = []

    async def complete(self, messages, **kwargs):
        self.requests.append(messages)
        return "好"


class _Conversations:
    def recent(self, scope, limit=20):
        return []


class _People:
    def profile(self, group_id, user_id):
        return {}


class _Memories:
    def related(self, scope, query, limit=4):
        return []


class _Skills:
    def catalog_text(self):
        return "目录"

    def select(self, text, *, proactive=False):
        return []


class _Tools:
    def schemas(self):
        return []


class _RaisingExperience:
    def context_for(self, query):
        raise RuntimeError("snapshot went bad")


class AgentIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.graph = write_graph(root / "graph")
        self.persona = root / "persona.md"
        self.persona.write_text("你是群友。", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self, behavior_experience=None):
        return Agent(
            _Model(),
            _Conversations(),
            _People(),
            _Memories(),
            _Skills(),
            _Tools(),
            self.persona,
            None,
            behavior_experience=behavior_experience,
        )

    def _experience(self):
        with env(BOT_BEHAVIOR_GRAPH_PATH=str(self.graph)):
            return configured_behavior_experience()

    def test_hint_lands_in_the_dynamic_suffix(self):
        agent = self._agent(self._experience())
        messages = agent.build_messages(event(text="有人问复习资料在哪里找"))
        self.assertIn("行为经验", messages[-1]["content"])

    def test_hint_never_enters_the_stable_prefix(self):
        """Enabling the graph must not change one byte of the cached prefix."""
        plain = self._agent(None)
        aware = self._agent(self._experience())
        self.assertEqual(plain.stable_prefix(), aware.stable_prefix())
        self.assertEqual(
            plain.build_messages(event(text="有人问复习资料"))[0],
            aware.build_messages(event(text="有人问复习资料"))[0],
        )

    def test_a_broken_read_model_costs_only_its_own_line(self):
        agent = self._agent(_RaisingExperience())
        messages = agent.build_messages(event(text="有人问复习资料"))
        self.assertTrue(messages)
        self.assertNotIn("行为经验", messages[-1]["content"])

    def test_no_snapshot_means_no_behavior_block(self):
        agent = self._agent(None)
        messages = agent.build_messages(event(text="有人问复习资料"))
        self.assertNotIn("行为经验", messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
