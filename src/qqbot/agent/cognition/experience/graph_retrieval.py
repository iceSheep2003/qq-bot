"""Evidence-path retrieval directly over the compiled behavior graph."""

from __future__ import annotations

import json
import math
import re
from array import array
from collections import defaultdict
from pathlib import Path

from qqbot.agent.cognition.experience.retrieval import BehaviorOption, _HINTS, _terms


_UNSAFE = (
    re.compile(r"一连串.{0,12}问题"),
    re.compile(r"连续(?:地)?追问|反复追问|连续提问"),
    re.compile(r"不断(?:地)?提问|每次都提问"),
)

#: How hard the ranking is pulled toward the graph's own strategy frequency, as
#: ``log(prior) + weight * normalised_score``.
#:
#: Measured, not guessed. Over 2007 cases under 5-fold session cross-validation
#: the *un-calibrated* scorer reaches 26.4% top-1 while the constant predictor
#: ("always answer the majority strategy") reaches 34.7%; at weight 1.5 the same
#: scorer reaches 35.8%, which beats the constant predictor by 1.1 points at
#: p=0.002 with all five folds agreeing on the direction.
#:
#: But that is the *micro* story, and micro accuracy is dominated by the 35%
#: majority class. On macro recall the same calibration *hurts*: 8.2% against
#: 14.9% for the raw scorer, because pulling the ranking toward one strategy
#: zeroes the recall of every other class. The hint this read model feeds the
#: prompt is supposed to suggest a *situationally appropriate* stance, so a
#: collapse to "supplement" adds no information the model did not already have.
#:
#: The default is therefore ``None`` — calibration off. It stays available for
#: callers whose objective really is "name the single most likely move", and the
#: weight is still read from ``BOT_BEHAVIOR_PRIOR_WEIGHT``.
DEFAULT_PRIOR_WEIGHT: float | None = None

#: Which half of each case the query is matched against. ``"interaction"`` keeps
#: only the interactional half (observable cues plus what the bot did); ``"topic"``
#: keeps the topical half (what the room was discussing); ``"full"`` keeps both.
#:
#: ``"interaction"`` is the default because it is the only setting that beats the
#: shipped scorer on *both* micro and macro accuracy (29.3% / 17.0% against
#: 26.4% / 14.9%). The topical half is not just unhelpful, it is noise: matching
#: "what was being discussed" against a query of raw room messages pulls the
#: ranking toward whichever case shares the most common words.
DEFAULT_FIELDS = "interaction"
#: Weight matched terms by ``log(N / df)`` instead of counting them. Common
#: bigrams swamp rare, discriminative ones under a plain count; IDF is the
#: standard correction and lifts both micro (27.9% vs 26.4%) and macro (15.7% vs
#: 14.9%).
DEFAULT_USE_IDF = True
#: The three hand-written boosts, all measured as neutral-to-harmful on the
#: held-out set and therefore off by default. ``hint_union`` folds the strategy
#: hint table's terms into the overlap; ``intent_bonus`` and ``family_bonus`` add
#: fixed constants from the query's keyword tables. Turning all three off is
#: worth ~1.7 points of top-1 (28.1% vs 26.4%).
DEFAULT_HINT_UNION = False
DEFAULT_INTENT_BONUS = False
DEFAULT_FAMILY_BONUS = False

#: Fine strategy key -> coarse behavior class.
#:
#: The compiled graph labels 13 strategies, but several of them are not
#: separable in practice: ``supplement``/``acknowledge``/``agree`` all mean
#: "just add something", ``clarify``/``answer_directly`` both mean "address the
#: question", and ``empathize``/``encourage`` both mean "support the mood".
#: Collapsing them is *measured*, not stylistic — under 5-fold session
#: cross-validation it lifts held-out top-1 from 29.3% to 40.3% (+11.0pt),
#: balanced accuracy from 17.0% to 23.1% and any@3 from 63.7% to 79.5%.
#:
#: The gain is *larger* than the oracle ceiling moves (+8.3pt), which is the
#: tell that the retriever's confusions were concentrated exactly inside these
#: groups: the classes being merged are the classes being confused.
#:
#: ``supplement`` is the worst offender — 34.7% of the corpus and the single
#: most frequent strategy in six of the nine situation families, which is what a
#: catch-all label looks like.
#:
#: The fine key is still read per row (it drives the hint table and the dialogue
#: act); only the grouping and the reported class are coarse. ``None`` restores
#: the raw 13-class behaviour.
DEFAULT_TAXONOMY: dict[str, str] = {
    "supplement": "附和",
    "acknowledge": "附和",
    "agree": "附和",
    "clarify": "答疑",
    "answer_directly": "答疑",
    "empathize": "情绪支持",
    "encourage": "情绪支持",
    "banter": "玩梗",
    "tease_gently": "轻调侃",
    "topic_shift": "转话题",
    "deescalate": "降温",
    "abstain": "不参与",
    "celebrate": "庆祝",
}


class BehaviorGraphRetriever:
    """Read a compiled graph and return only complete evidence paths.

    This adapter is read-only.  It requires the case, strategy, action,
    expression, media and evidence links to share one evidence id; unrelated
    graph branches are never combined into one option.
    """

    def __init__(
        self,
        snapshot: Path | str,
        calibration: Path | str | None = None,
        prior_weight: float | None = DEFAULT_PRIOR_WEIGHT,
        *,
        fields: str = DEFAULT_FIELDS,
        use_idf: bool = DEFAULT_USE_IDF,
        hint_union: bool = DEFAULT_HINT_UNION,
        intent_bonus: bool = DEFAULT_INTENT_BONUS,
        family_bonus: bool = DEFAULT_FAMILY_BONUS,
        taxonomy: dict[str, str] | None = DEFAULT_TAXONOMY,
    ):
        self.snapshot = Path(snapshot)
        # ``None`` switches the calibration off and leaves the raw overlap
        # ranking; ``0.0`` is not "off" but the far end of the scale — pure
        # prior, i.e. always answer the majority strategy. Those are different
        # behaviours, so the sentinel has to be distinct from zero.
        self.prior_weight = (
            None if prior_weight is None else max(0.0, float(prior_weight))
        )
        if fields not in ("interaction", "topic", "full"):
            raise ValueError(f"unknown field set: {fields!r}")
        self.fields = fields
        self.use_idf = bool(use_idf)
        self.hint_union = bool(hint_union)
        self.intent_bonus = bool(intent_bonus)
        self.family_bonus = bool(family_bonus)
        # ``None`` keeps the raw 13-class labels; a table collapses them.
        self.taxonomy = dict(taxonomy) if taxonomy else None
        manifest = self.snapshot / "manifest.json"
        nodes_path = self.snapshot / "nodes.jsonl"
        edges_path = self.snapshot / "edges.jsonl"
        if not all(path.is_file() for path in (manifest, nodes_path, edges_path)):
            raise ValueError("compiled behavior graph is incomplete")
        self.manifest = json.loads(manifest.read_text(encoding="utf-8"))
        with nodes_path.open(encoding="utf-8") as stream:
            self.nodes = {node["id"]: node for line in stream if (node := json.loads(line))}
        with edges_path.open(encoding="utf-8") as stream:
            self.edges = [json.loads(line) for line in stream if line.strip()]
        self.by_evidence: defaultdict[str, list[dict]] = defaultdict(list)
        self.outgoing: defaultdict[tuple[str, str], list[dict]] = defaultdict(list)
        for edge in self.edges:
            self.by_evidence[edge["evidence_id"]].append(edge)
            self.outgoing[(edge["source"], edge["relation"])].append(edge)
        self._hints = {key: _terms(value) for key, value in _HINTS.items()}
        self.calibration = {}
        if calibration:
            self.calibration = json.loads(Path(calibration).read_text(encoding="utf-8")).get("family_strategy_bonus", {})
        self.case_paths = self._build_paths()
        self._build_index()

    def _searchable(self, path) -> str:
        """The static text a case path is matched against.

        Every field here comes from the compiled snapshot, so the result is
        immutable for the lifetime of the process — which is what makes
        pre-tokenising it in :meth:`_build_index` safe.

        The two halves are split deliberately. The *topical* half says what the
        room was discussing; the *interactional* half says what was observed and
        what the bot did. The label is interactional, so which half the index is
        built from is a first-class choice, not a detail — see ``DEFAULT_FIELDS``.
        """
        case, strategy, action, expression, _media, situation, _evidence = path
        parts: list[str] = []
        if self.fields in ("full", "topic"):
            parts += [
                case.get("situation_text", ""),
                situation.get("name", ""),
                situation.get("family_key", ""),
                *case.get("topic_terms", []),
            ]
        if self.fields in ("full", "interaction"):
            parts += [
                *case.get("observable_cues", []),
                strategy.get("key", ""),
                action.get("key", ""),
                expression.get("name", ""),
                *expression.get("raw_variants", [])[:5],
            ]
        return " ".join(parts)

    def _build_index(self) -> None:
        """Tokenise every case path once, at load, into an inverted index.

        ``retrieve`` runs inside a turn, on the event loop. Rebuilding and
        re-tokenising ~2000 searchable strings on every call cost ~157 ms per
        turn, which is why this read model had no caller: the cost was paid on
        the hot path. The text never changes after load, so the tokenisation is
        paid once here instead, and a turn costs a few milliseconds.

        Postings are appended while walking paths in order, so every posting
        list is already sorted and needs no separate sort pass.
        """
        postings: defaultdict[str, list[int]] = defaultdict(list)
        for index, path in enumerate(self.case_paths):
            for term in _terms(self._searchable(path)):
                postings[term].append(index)
        self._postings = {term: array("i", rows) for term, rows in postings.items()}
        # ``log(N / df)`` per term, so the scorer can sum weights instead of
        # counting overlaps. A term carried by most cases says almost nothing
        # about which case fits; a rare one says a lot.
        total_paths = len(self.case_paths) or 1
        self._idf = {
            term: math.log(total_paths / len(rows)) for term, rows in postings.items()
        }
        # Strategy hints are the only terms consulted *per path* ("does this
        # case carry this hint term?"), so those get a set for O(1) membership.
        # The set is bounded by the 13 hint rows, not by the whole vocabulary.
        hint_terms = {term for terms in self._hints.values() for term in terms}
        self._hint_postings = {
            term: set(self._postings[term])
            for term in hint_terms
            if term in self._postings
        }
        # Strategy frequency across the graph, for the ranking's prior term.
        # Rebuilt here rather than in ``__init__`` because callers that hold out
        # a slice of the graph reassign ``case_paths`` and rebuild the index —
        # the prior has to follow the paths it is derived from, or it would leak
        # the held-out distribution into the ranking.
        counts: defaultdict[str, int] = defaultdict(int)
        for _case, strategy, _action, _expression, _media, _situation, _evidence in self.case_paths:
            counts[self._class_of(strategy.get("key", ""))] += 1
        total = sum(counts.values()) or 1
        self._prior = {key: count / total for key, count in counts.items()}

    def _class_of(self, strategy_key: str) -> str:
        """Map a fine strategy key to the coarse class the ranking groups by."""
        if not self.taxonomy:
            return strategy_key
        return self.taxonomy.get(strategy_key, strategy_key)

    def _build_paths(self):
        paths = []
        for case in self.nodes.values():
            if case.get("type") != "InteractionCase":
                continue
            evidence_edges = self.outgoing[(case["id"], "has_evidence")]
            for has_evidence in evidence_edges:
                evidence_id = has_evidence["target"]
                links = self.by_evidence.get(evidence_id, [])
                targets = {}
                for edge in links:
                    if edge["source"] == case["id"]:
                        targets[edge["relation"]] = self.nodes.get(edge["target"])
                strategy = targets.get("supports")
                action = targets.get("realizes_action")
                expression = targets.get("expressed_as")
                media = targets.get("may_use")
                situation = targets.get("observed_in")
                if not all((strategy, action, expression, media, situation)):
                    continue
                raw_variants = expression.get("raw_variants", [])
                if any(pattern.search(" ".join(raw_variants)) for pattern in _UNSAFE):
                    continue
                paths.append((case, strategy, action, expression, media, situation, evidence_id))
        return paths

    def _paths(self):
        return iter(self.case_paths)

    def retrieve(self, query: str, *, limit: int = 3) -> list[BehaviorOption]:
        if not isinstance(query, str) or not query.strip():
            return []
        limit = max(1, min(3, int(limit)))
        query_terms = _terms(query)
        # The three hand-written boosts are computed only when enabled: each
        # consults a keyword table, and all three are off by default because the
        # held-out evaluation measured them as neutral-to-harmful.
        intent_bonus = self._intent_bonus(query_terms) if self.intent_bonus else {}
        family_bonus = (
            self.calibration.get(self._query_family(query), {})
            if self.family_bonus
            else {}
        )
        # The matched terms for every path, in one pass over postings. This is
        # the same set intersection the previous implementation did per path;
        # the index just answers it without re-tokenising the case.
        matched: defaultdict[int, list[str]] = defaultdict(list)
        for term in query_terms:
            for index in self._postings.get(term, ()):
                matched[index].append(term)
        grouped: defaultdict[str, list[tuple[float, dict]]] = defaultdict(list)
        for index, (
            case,
            strategy,
            action,
            expression,
            media,
            situation,
            evidence_id,
        ) in enumerate(self.case_paths):
            strategy_key = strategy.get("key", "")
            terms = list(matched.get(index, ()))
            # A strategy hint contributes query terms the case text itself does
            # not carry, so only the ones this path is missing are added.
            if self.hint_union:
                for term in self._hints.get(strategy_key, ()):
                    if term in query_terms and index not in self._hint_postings.get(term, ()):
                        terms.append(term)
            if not terms:
                continue
            if self.use_idf:
                score = sum(self._idf.get(term, 0.0) for term in terms)
            else:
                score = float(len(terms))
            score += intent_bonus.get(strategy_key, 0.0)
            score += float(family_bonus.get(strategy_key, 0.0))
            # Group by the *coarse class*, not the strategy node id: the whole
            # point of the taxonomy is that several fine strategies collapse
            # into one choice, and grouping is what decides which choices are
            # offered at all.
            grouped[self._class_of(strategy_key)].append(
                (
                    score,
                    {
                        "case": case,
                        "strategy": strategy,
                        "action": action,
                        "expression": expression,
                        "media": media,
                        "situation": situation,
                        "evidence_id": evidence_id,
                    },
                )
            )
        ranked = []
        for class_key, rows in grouped.items():
            rows.sort(key=lambda item: (-item[0], item[1]["case"].get("case_id", "")))
            best = rows[0][1]
            strategy = best["strategy"]
            expression = best["expression"]
            examples = []
            for _, item in rows[:2]:
                examples.append(
                    {
                        "case_id": item["case"].get("case_id"),
                        "situation_family": item["situation"].get("family_key"),
                        "situation": item["case"].get("situation_text", ""),
                        "topic_terms": item["case"].get("topic_terms", []),
                        "evidence_id": item["evidence_id"],
                    }
                )
            # Support is summed across every fine strategy folded into this
            # class: the class stands for all of them, so it should report their
            # combined support rather than whichever node happened to rank first.
            support = 0
            seen_nodes: set[str] = set()
            for _, item in rows:
                node_id = item["strategy"]["id"]
                if node_id in seen_nodes:
                    continue
                seen_nodes.add(node_id)
                support += int(item["strategy"].get("support_case_count", 0) or 0)
            ranked.append(
                (
                    best_score := sum(item[0] for item in rows[:3]) / min(3, len(rows)),
                    BehaviorOption(
                        strategy=class_key,
                        dialogue_act=best["action"].get("key", "wait"),
                        expression_pattern=expression.get("name", ""),
                        avoid=tuple(expression.get("avoid", [])[:6]),
                        media_intent=best["media"].get("key", "none"),
                        support_case_count=support or len(rows),
                        examples=tuple(examples),
                        score=best_score,
                        snapshot_version=str(self.manifest.get("pipeline_version", "unknown")),
                    ),
                )
            )
        if self.prior_weight is not None:
            # ``log(prior) + weight * normalised_retrieval``. Normalising against
            # this query's own best score keeps the term scale-free, so the
            # weight means the same thing regardless of how long the query was.
            # ``None`` is "off"; ``0.0`` is pure prior (constant predictor), which
            # is a real, distinct behaviour and must not be collapsed into "off".
            #
            # Off by default. Calibration is a *micro*-accuracy tool: it lifts
            # top-1 by pulling the ranking toward the 35% majority class, and in
            # doing so collapses the hint to a constant. Since the prompt uses the
            # hint to suggest a stance, that trades away exactly the diversity it
            # is there for — macro recall falls from 14.9% to 8.2%. Callers that
            # really want "name the single most likely move" can turn it back on.
            ceiling = max((item[0] for item in ranked), default=0.0) or 1.0
            ranked = [
                (
                    self.prior_weight * (score / ceiling)
                    + math.log(self._prior.get(option.strategy, 1e-6)),
                    option,
                )
                for score, option in ranked
            ]
        ranked.sort(key=lambda item: (-item[0], -item[1].support_case_count, item[1].strategy))
        return [item[1] for item in ranked[:limit]]

    @staticmethod
    def _intent_bonus(query_terms: set[str]) -> dict[str, float]:
        bonus = defaultdict(float)
        if query_terms & {"资料", "求助", "怎么", "询问", "答案", "推荐"}:
            bonus["answer_directly"] += 1.5
            bonus["clarify"] += 0.8
            bonus["supplement"] += 0.4
        if query_terms & {"调侃", "玩笑", "梗", "幽默", "开玩"}:
            bonus["banter"] += 1.5
            bonus["tease_gently"] += 0.8
        if query_terms & {"争论", "冲突", "缓和", "误解", "生气"}:
            bonus["deescalate"] += 2.0
        if query_terms & {"累", "焦虑", "崩", "难受", "失望"}:
            bonus["empathize"] += 1.5
            bonus["encourage"] += 0.6
        return dict(bonus)

    @staticmethod
    def _query_family(query: str) -> str:
        text = query.lower()
        markers = (
            ("banter_or_joke", ("调侃", "玩笑", "梗", "哈哈", "夸张")),
            ("emotional_complaint", ("焦虑", "难受", "崩", "放弃", "压力", "累")),
            ("clarification_or_dispute", ("争论", "误解", "区别", "定义", "质疑", "为什么")),
            ("direct_question", ("怎么", "询问", "求助", "资料", "推荐", "谁有", "吗")),
            ("progress_or_good_news", ("进步", "成功", "好消息", "成绩", "上岸")),
            ("topic_shift_or_cold_room", ("换个话题", "冷场", "转到")),
        )
        for family, values in markers:
            if any(value in text for value in values):
                return family
        return "general_group_exchange"
