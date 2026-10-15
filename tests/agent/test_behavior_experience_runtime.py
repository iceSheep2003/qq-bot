"""behavior experience: two selectable routes into the reply pipeline.

There are two ways to decide how the bot takes part in a turn — the model alone,
and the behavior graph — and ``BOT_BEHAVIOR_MODE`` picks one. ``app.py`` hands
``configured_behavior_decision()`` to the Agent, and ``Agent.build_messages``
injects whatever the route decides into the *dynamic suffix*. These tests pin
the properties that make that safe:

*the route is optional* — no snapshot, a broken one, or an unknown mode all fall
back to the model-alone route without stopping the bot;

*the two routes cannot be confused* — the advisory route writes ``行为经验`` with
``derived_read_only`` authority, the binding route writes ``本轮行为决策`` with
``binding``, so a prompt never has to guess how seriously to take a block;

*it never touches the stable prefix* — enabling either route must not change one
byte of the cached prefix, or every turn pays full price again;

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

from qunbot.contracts.models import MessageEvent
from qunbot.agent.cognition.experience import graph_retrieval
from qunbot.agent.cognition.experience.graph_retrieval import DEFAULT_TAXONOMY, BehaviorGraphRetriever
from qunbot.agent.cognition.experience.retrieval import _terms
from qunbot.agent.cognition.experience.runtime import (
    CompiledBehaviorExperience,
    configured_behavior_experience,
)
from qunbot.agent.cognition.experience.decision import configured_behavior_decision
from qunbot.agent.orchestrator import Agent

_ENV_KEYS = (
    "BOT_BEHAVIOR_MODE",
    "BOT_BEHAVIOR_GRAPH_PATH",
    "BOT_BEHAVIOR_CALIBRATION_PATH",
    "BOT_BEHAVIOR_MAX_OPTIONS",
    "BOT_BEHAVIOR_MAX_CASES",
    "BOT_BEHAVIOR_CHAR_BUDGET",
    "BOT_BEHAVIOR_PRIOR_WEIGHT",
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
#:
#: ``cues`` carry the interactional half the retriever actually indexes by
#: default (``fields="interaction"``); ``situation``/``topics`` carry the topical
#: half, which is deliberately *not* matched. Real compiled cases look the same
#: way: ``observable_cues`` describe what happened, ``topic_terms`` describe what
#: was being discussed.
FIXTURE_CASES = [
    {
        "situation": "有人问考研复习资料在哪里找",
        "cues": ["有人发消息问复习资料在哪找", "有人点名求推荐复习资料"],
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
        "cues": ["有人用夸张说法开玩笑", "有人顺着起哄接梗"],
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
        "cues": ["有人抱怨最近压力很大", "有人透露出想放弃"],
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

    Chain nodes are *shared by key*, exactly as the compiler emits them: a real
    snapshot has one ``Strategy`` node per strategy, not one per case. Grouping
    in ``retrieve`` is by strategy node id, so a per-case node would let the same
    strategy occupy several of the returned slots — a fixture artefact, not a
    behaviour the shipped graph can exhibit.
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    root.mkdir(parents=True, exist_ok=True)
    # natural key per relation, so equivalent nodes collapse into one
    chain_key = {
        "supports": lambda case: case["strategy"],
        "realizes_action": lambda case: case["act"],
        "expressed_as": lambda case: case["expression"],
        "may_use": lambda case: case.get("media", "none"),
        "observed_in": lambda case: case.get("family", "general_group_exchange"),
    }
    chain_node: dict[tuple[str, str], dict] = {}
    chain_support: dict[str, int] = {}
    for case in cases if cases is not None else FIXTURE_CASES:
        chain_support[case["strategy"]] = chain_support.get(case["strategy"], 0) + 1

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
        specs = {
            "supports": {
                "type": "Strategy",
                "key": case["strategy"],
                "support_case_count": chain_support[case["strategy"]],
            },
            "realizes_action": {
                "type": "DialogueAct",
                "key": case["act"],
            },
            "expressed_as": {
                "type": "ExpressionPattern",
                "name": case["expression"],
                "raw_variants": case.get("variants", []),
                "avoid": case.get("avoid", []),
            },
            "may_use": {
                "type": "MediaIntent",
                "key": case.get("media", "none"),
            },
            "observed_in": {
                "type": "Situation",
                "family_key": case.get("family", "general_group_exchange"),
                "name": case["situation"],
            },
        }
        for relation, spec in specs.items():
            natural = chain_key[relation](case)
            node = chain_node.get((relation, natural))
            if node is None:
                node = {"id": f"{relation[:2]}{len(chain_node)}", **spec}
                chain_node[(relation, natural)] = node
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

    def test_direct_question_matches_the_answer_class(self):
        retriever = BehaviorGraphRetriever(self.root)
        options = retriever.retrieve("有人问复习资料在哪里找", limit=3)
        self.assertEqual(options[0].strategy, "答疑")
        self.assertEqual(options[0].dialogue_act, "answer")

    def test_joke_matches_the_banter_class(self):
        retriever = BehaviorGraphRetriever(self.root)
        options = retriever.retrieve("哈哈哈这个玩笑太夸张了", limit=3)
        self.assertEqual(options[0].strategy, "玩梗")

    def test_complaint_matches_the_empathize_class(self):
        retriever = BehaviorGraphRetriever(self.root)
        options = retriever.retrieve("最近压力好大想放弃", limit=3)
        self.assertEqual(options[0].strategy, "情绪支持")

    def test_indexed_scoring_matches_a_direct_scan(self):
        """The optimisation must not move a single score.

        This is the invariant the 65x speed-up was accepted under: the index
        only changes *when* a case is tokenised, never what it scores. It is
        checked against the shipped default scorer — interactional fields,
        IDF-weighted, no hand-written boosts, grouped by the coarse taxonomy.
        """
        retriever = BehaviorGraphRetriever(self.root)
        for query in (
            "有人问复习资料在哪里找",
            "哈哈哈这个玩笑太夸张了",
            "最近压力好大想放弃",
            "复习 玩笑 压力 一起说",
        ):
            query_terms = _terms(query)
            expected = {}
            for case, strategy, action, expression, media, situation, evidence in retriever.case_paths:
                searchable = retriever._searchable(
                    (case, strategy, action, expression, media, situation, evidence)
                )
                terms = query_terms & _terms(searchable)
                if not terms:
                    continue
                score = sum(retriever._idf.get(term, 0.0) for term in terms)
                key = retriever._class_of(strategy.get("key", ""))
                best = expected.get(key)
                if best is None or score > best:
                    expected[key] = score

            produced = retriever.retrieve(query, limit=3)
            for option in produced:
                self.assertIn(option.strategy, expected, f"{option.strategy} scored nothing")
                # The reported score averages the best few rows of the class, so
                # it is never above the single best row the scan found.
                self.assertLessEqual(round(option.score, 9), round(expected[option.strategy], 9))


# ------------------------------------------------------- prior calibration


def skewed_graph(root: Path) -> Path:
    """Five bland cases and one distinctive one, so the two forces disagree.

    The distinctive banter case matches the query on its own terms (``玩笑``,
    ``夸张``); the bland majority only shares the single token ``玩笑`` but there
    is far more of it. Both must survive the overlap filter for the calibration
    weight to have anything to decide between — a zero-overlap strategy is
    dropped before blending, which would make the test trivially pass for the
    wrong reason.
    """
    cases = [
        {
            "situation": "有人随口补一句玩笑话",
            "cues": ["有人随口补一句玩笑话"],
            "topics": ["日常"],
            "strategy": "supplement",
            "act": "add",
            "expression": "顺口补充",
            "variants": ["随口接一句"],
            "family": "general_group_exchange",
            "support": 5,
        }
        for _ in range(5)
    ]
    cases.append(
        {
            "situation": "有人用夸张说法开玩笑起哄",
            "cues": ["有人用夸张说法开玩笑起哄"],
            "topics": ["玩笑", "调侃"],
            "strategy": "banter",
            "act": "joke",
            "expression": "用夸张说法接梗",
            "variants": ["夸张地顺着接一句"],
            "family": "banter_or_joke",
            "support": 1,
        }
    )
    return write_graph(root, cases)


class PriorCalibrationTests(unittest.TestCase):
    """The ranking is pulled toward the graph's own strategy frequency.

    Measured on the real corpus: the un-calibrated scorer is *worse than always
    answering the majority strategy* (26.4% vs 34.7% top-1 over 2007 held-out
    cases, p<0.001), and calibrating it reaches 35.8% at p=0.002. These tests
    pin the mechanism, not the corpus.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = skewed_graph(Path(self._tmp.name))
        self.query = "这个玩笑太夸张了哈哈"

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_prior_is_the_graphs_own_strategy_frequency(self):
        retriever = BehaviorGraphRetriever(self.root)
        self.assertEqual(retriever._prior["附和"], 5 / 6)
        self.assertEqual(retriever._prior["玩梗"], 1 / 6)

    def test_zero_weight_is_the_constant_predictor(self):
        """``prior_weight=0`` collapses to ``log(prior)`` alone, so the ranking
        is the graph's own strategy frequency and ignores the query entirely —
        that is the constant predictor ("always answer the majority strategy")
        and exactly what the benchmark's λ=0 reference must reproduce.

        The equivalence proof for the index optimisation is a separate concern
        (see ``verify_retrieval_equivalence.py``) and does not depend on this.
        """
        retriever = BehaviorGraphRetriever(self.root, prior_weight=0)
        options = retriever.retrieve(self.query, limit=3)
        self.assertEqual(options[0].strategy, "附和")
        # All returned options are ordered by prior, majority first.
        ranks = [
            (retriever._prior[option.strategy], option.strategy)
            for option in options
        ]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_a_large_weight_lets_the_retrieval_signal_win(self):
        """At a large weight the ranking follows the query's own retrieval
        score, so the case that actually matches the query (``banter``) beats
        the majority strategy (``supplement``) that merely carries the higher
        base rate. This is the opposite end of the scale from
        :meth:`test_zero_weight_is_the_constant_predictor`.
        """
        retriever = BehaviorGraphRetriever(self.root, prior_weight=500.0)
        options = retriever.retrieve(self.query, limit=3)
        self.assertEqual(options[0].strategy, "玩梗")

    def test_the_default_weight_is_off(self):
        """Micro accuracy peaks at λ=1.5, but macro recall peaks at λ=0. Since
        the hint exists to suggest a *stance*, the default ships calibration off
        and the sweep's optimum stays reachable through the constructor."""
        self.assertIsNone(BehaviorGraphRetriever(self.root).prior_weight)
        self.assertEqual(BehaviorGraphRetriever(self.root, prior_weight=1.5).prior_weight, 1.5)

    def test_a_negative_weight_is_clamped_rather_than_inverting_the_ranking(self):
        self.assertEqual(BehaviorGraphRetriever(self.root, prior_weight=-3).prior_weight, 0.0)

    def test_the_prior_follows_the_paths_it_is_derived_from(self):
        """Callers that hold out a slice of the graph reassign ``case_paths``.

        If the prior were computed once in ``__init__`` it would keep describing
        the full graph, which leaks the held-out distribution into the ranking
        of the very cases being held out.
        """
        retriever = BehaviorGraphRetriever(self.root)
        self.assertEqual(retriever._prior["玩梗"], 1 / 6)

        retriever.case_paths = [
            path for path in retriever.case_paths if path[1].get("key") != "banter"
        ]
        retriever._build_index()

        self.assertEqual(retriever._prior, {"附和": 1.0})

    def test_the_reported_score_stays_the_retrieval_score(self):
        """The calibration decides the order; it does not rewrite the score the
        compiler and the console read."""
        calibrated = BehaviorGraphRetriever(self.root)
        plain = BehaviorGraphRetriever(self.root, prior_weight=0)
        by_strategy = {
            option.strategy: option.score for option in plain.retrieve(self.query, limit=3)
        }
        for option in calibrated.retrieve(self.query, limit=3):
            self.assertAlmostEqual(option.score, by_strategy[option.strategy], places=9)


class PriorWeightConfigurationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = write_graph(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_weight_is_read_from_the_environment(self):
        with env(BOT_BEHAVIOR_GRAPH_PATH=self.root, BOT_BEHAVIOR_PRIOR_WEIGHT="0"):
            self.assertEqual(configured_behavior_experience().retriever.prior_weight, 0.0)

    def test_the_default_disables_calibration(self):
        """Calibration is a micro-accuracy tool that collapses the hint to the
        majority strategy; it is opt-in, so the default is off."""
        with env(BOT_BEHAVIOR_GRAPH_PATH=self.root):
            self.assertIsNone(configured_behavior_experience().retriever.prior_weight)

    def test_the_weight_can_be_switched_off_explicitly(self):
        with env(BOT_BEHAVIOR_GRAPH_PATH=self.root, BOT_BEHAVIOR_PRIOR_WEIGHT="off"):
            self.assertIsNone(configured_behavior_experience().retriever.prior_weight)

    def test_a_malformed_weight_disables_the_graph_rather_than_crashing(self):
        """Same contract as every other optional snapshot setting: a bad value
        turns the read model off and leaves the bot running."""
        with env(BOT_BEHAVIOR_GRAPH_PATH=self.root, BOT_BEHAVIOR_PRIOR_WEIGHT="high"):
            self.assertIsNone(configured_behavior_experience())


class ScoringDefaultsTests(unittest.TestCase):
    """The shipped scorer is the one the held-out evaluation selected.

    Every default here is a measured choice, not a taste: the interactional
    field set and IDF weighting are the only changes that beat the old scorer on
    *both* micro and macro accuracy, and the three hand-written boosts are
    neutral-to-harmful, so all three are off.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = write_graph(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults_are_the_measured_best(self):
        retriever = BehaviorGraphRetriever(self.root)
        self.assertEqual(retriever.fields, "interaction")
        self.assertTrue(retriever.use_idf)
        self.assertFalse(retriever.hint_union)
        self.assertFalse(retriever.intent_bonus)
        self.assertFalse(retriever.family_bonus)
        self.assertIsNone(retriever.prior_weight)

    def test_the_topical_half_is_not_indexed_by_default(self):
        """``situation_text`` and ``topic_terms`` are topical; a query that only
        shares them must not match, or the split is not real."""
        retriever = BehaviorGraphRetriever(self.root)
        # "考研" appears only in a case's situation_text/topics, never in its cues.
        self.assertEqual(retriever.retrieve("考研", limit=3), [])

    def test_the_interactional_half_is_indexed_by_default(self):
        retriever = BehaviorGraphRetriever(self.root)
        options = retriever.retrieve("有人发消息问复习资料在哪找", limit=3)
        self.assertEqual(options[0].strategy, "答疑")

    def test_unknown_field_set_is_rejected(self):
        with self.assertRaises(ValueError):
            BehaviorGraphRetriever(self.root, fields="topical")


class TaxonomyTests(unittest.TestCase):
    """Fine strategies are collapsed into coarse behavior classes.

    Measured, not stylistic: merging 13 classes into 9 lifts held-out top-1 from
    29.3% to 40.3% (+11.0pt) and balanced accuracy from 17.0% to 23.1%. These
    tests pin the mechanism — the *grouping* and the *reported class* are coarse,
    while the fine key still drives the hint table and the dialogue act.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = write_graph(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_default_taxonomy_collapses_the_ambiguous_groups(self):
        retriever = BehaviorGraphRetriever(self.root)
        for fine in ("supplement", "acknowledge", "agree"):
            self.assertEqual(retriever._class_of(fine), "附和")
        for fine in ("clarify", "answer_directly"):
            self.assertEqual(retriever._class_of(fine), "答疑")
        for fine in ("empathize", "encourage"):
            self.assertEqual(retriever._class_of(fine), "情绪支持")

    def test_the_default_taxonomy_covers_every_shipped_strategy(self):
        """Every key the graph emits must map, or the class set silently mixes
        Chinese and English and the taxonomy is only half applied."""
        retriever = BehaviorGraphRetriever(self.root)
        for _case, strategy, _a, _e, _m, _s, _ev in retriever.case_paths:
            key = strategy.get("key", "")
            self.assertIn(key, DEFAULT_TAXONOMY, f"{key} has no coarse class")
            self.assertEqual(retriever._class_of(key), DEFAULT_TAXONOMY[key])

    def test_disabling_the_taxonomy_restores_the_fine_labels(self):
        retriever = BehaviorGraphRetriever(self.root, taxonomy=None)
        options = retriever.retrieve("有人问复习资料在哪里找", limit=3)
        self.assertEqual(options[0].strategy, "answer_directly")
        self.assertEqual(retriever._class_of("answer_directly"), "answer_directly")

    def test_the_class_reports_the_combined_support(self):
        """The coarse class stands for several fine strategies, so it reports
        their summed support rather than one node's count."""
        cases = [
            {
                "situation": "有人随口补一句", "cues": ["有人随口补一句"],
                "strategy": "supplement", "act": "add", "expression": "顺口补充",
                "variants": ["随口接一句"], "family": "general_group_exchange",
            },
            {
                "situation": "有人附和一句", "cues": ["有人附和一句"],
                "strategy": "acknowledge", "act": "acknowledge", "expression": "表示收到",
                "variants": ["收到收到"], "family": "general_group_exchange",
            },
        ]
        root = write_graph(Path(self._tmp.name) / "g", cases)
        retriever = BehaviorGraphRetriever(root)
        options = retriever.retrieve("有人随口补一句", limit=3)
        self.assertEqual(options[0].strategy, "附和")
        self.assertEqual(options[0].support_case_count, 2)

    def test_an_unknown_strategy_key_passes_through_unchanged(self):
        retriever = BehaviorGraphRetriever(self.root)
        self.assertEqual(retriever._class_of("brand_new_strategy"), "brand_new_strategy")


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


class DecisionRouteTests(unittest.TestCase):
    """The route object itself: what each mode decides to contribute."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = write_graph(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _graph_route(self, mode):
        with env(BOT_BEHAVIOR_MODE=mode, BOT_BEHAVIOR_GRAPH_PATH=str(self.root)):
            route = configured_behavior_decision()
        self.assertEqual(route.mode, mode)
        return route

    def test_the_hint_route_is_advisory_and_may_offer_alternatives(self):
        decision = self._graph_route("hint").decide("有人问复习资料在哪里找")
        self.assertIsNotNone(decision)
        self.assertEqual(decision.key, "行为经验")
        self.assertEqual(decision.authority, "derived_read_only")
        self.assertEqual(decision.source, "graph")
        self.assertIn("行为经验候选", decision.block["content"])

    def test_the_graph_route_is_binding_and_speaks_about_one_option(self):
        """A decision that offered three alternatives would not be a decision."""
        decision = self._graph_route("graph").decide("有人问复习资料在哪里找")
        self.assertIsNotNone(decision)
        self.assertEqual(decision.key, "本轮行为决策")
        self.assertEqual(decision.authority, "binding")
        self.assertEqual(decision.block["策略"], "答疑")
        self.assertIn("表达结构", decision.block)
        self.assertNotIn("2. ", decision.block["content"])

    def test_a_query_with_no_match_decides_nothing(self):
        self.assertIsNone(self._graph_route("hint").decide("zzz 完全无关的内容 qqq"))

    def test_a_blank_query_decides_nothing(self):
        self.assertIsNone(self._graph_route("graph").decide("   "))


class _RaisingDecision:
    """A route that blows up. A reply must never depend on one of these."""

    mode = "hint"

    def decide(self, query):
        raise RuntimeError("snapshot went bad")


class AgentIntegrationTests(unittest.TestCase):
    """The two routes, and the promise that neither can break a reply."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.graph = write_graph(root / "graph")
        self.persona = root / "persona.md"
        self.persona.write_text("你是群友。", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _agent(self, behavior_decision=None):
        return Agent(
            _Model(),
            _Conversations(),
            _People(),
            _Memories(),
            _Skills(),
            _Tools(),
            self.persona,
            None,
            behavior_decision=behavior_decision,
        )

    def _route(self, mode):
        with env(BOT_BEHAVIOR_MODE=mode, BOT_BEHAVIOR_GRAPH_PATH=str(self.graph)):
            return configured_behavior_decision()

    def test_the_model_alone_route_adds_nothing_to_the_prompt(self):
        """``off`` is a real route, not an absence: it decides to say nothing."""
        route = self._route("off")
        self.assertEqual(route.mode, "off")
        self.assertIsNone(route.decide("有人问复习资料"))
        content = self._agent(route).build_messages(
            event(text="有人问复习资料在哪里找")
        )[-1]["content"]
        self.assertNotIn("行为经验", content)
        self.assertNotIn("本轮行为决策", content)

    def test_the_hint_route_adds_advisory_text(self):
        content = self._agent(self._route("hint")).build_messages(
            event(text="有人问复习资料在哪里找")
        )[-1]["content"]
        self.assertIn("行为经验", content)
        self.assertIn("derived_read_only", content)
        self.assertNotIn("本轮行为决策", content)

    def test_the_graph_route_binds_the_stance(self):
        """The binding route writes its own slot, so the two can never be
        confused for one another in a prompt or a transcript."""
        content = self._agent(self._route("graph")).build_messages(
            event(text="有人问复习资料在哪里找")
        )[-1]["content"]
        self.assertIn("本轮行为决策", content)
        self.assertIn("binding", content)
        self.assertNotIn("行为经验", content)

    def test_an_unknown_mode_falls_back_to_the_model_alone(self):
        self.assertEqual(self._route("nonsense").mode, "off")

    def test_a_mode_without_a_snapshot_falls_back_to_the_model_alone(self):
        """A route is optional equipment; a broken snapshot must not stop the
        bot, it must simply leave the model to decide."""
        with env(BOT_BEHAVIOR_MODE="graph", BOT_BEHAVIOR_GRAPH_PATH=""):
            route = configured_behavior_decision()
        self.assertEqual(route.mode, "off")

    def test_no_route_enters_the_stable_prefix(self):
        """Whichever route is on, the cached prefix must not move a byte."""
        plain = self._agent(None)
        for mode in ("off", "hint", "graph"):
            aware = self._agent(self._route(mode))
            self.assertEqual(plain.stable_prefix(), aware.stable_prefix())
            self.assertEqual(
                plain.build_messages(event(text="有人问复习资料"))[0],
                aware.build_messages(event(text="有人问复习资料"))[0],
            )

    def test_a_broken_route_costs_only_its_own_line(self):
        messages = self._agent(_RaisingDecision()).build_messages(
            event(text="有人问复习资料")
        )
        self.assertTrue(messages)
        self.assertNotIn("行为经验", messages[-1]["content"])
        self.assertNotIn("本轮行为决策", messages[-1]["content"])

    def test_no_route_means_no_behavior_block(self):
        content = self._agent(None).build_messages(event(text="有人问复习资料"))[-1]["content"]
        self.assertNotIn("行为经验", content)
        self.assertNotIn("本轮行为决策", content)


if __name__ == "__main__":
    unittest.main()
