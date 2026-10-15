"""Evidence-path retrieval directly over the compiled behavior graph."""

from __future__ import annotations

import json
import re
from array import array
from collections import defaultdict
from pathlib import Path

from .retrieval import BehaviorOption, _HINTS, _terms


_UNSAFE = (
    re.compile(r"一连串.{0,12}问题"),
    re.compile(r"连续(?:地)?追问|反复追问|连续提问"),
    re.compile(r"不断(?:地)?提问|每次都提问"),
)


class BehaviorGraphRetriever:
    """Read a compiled graph and return only complete evidence paths.

    This adapter is read-only.  It requires the case, strategy, action,
    expression, media and evidence links to share one evidence id; unrelated
    graph branches are never combined into one option.
    """

    def __init__(self, snapshot: Path | str, calibration: Path | str | None = None):
        self.snapshot = Path(snapshot)
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

    @staticmethod
    def _searchable(path) -> str:
        """The static text a case path is matched against.

        Every field here comes from the compiled snapshot, so the result is
        immutable for the lifetime of the process — which is what makes
        pre-tokenising it in :meth:`_build_index` safe.
        """
        case, strategy, action, expression, _media, situation, _evidence = path
        return " ".join(
            [
                case.get("situation_text", ""),
                situation.get("name", ""),
                situation.get("family_key", ""),
                *case.get("observable_cues", []),
                *case.get("topic_terms", []),
                strategy.get("key", ""),
                action.get("key", ""),
                expression.get("name", ""),
                *expression.get("raw_variants", [])[:5],
            ]
        )

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
        # Strategy hints are the only terms consulted *per path* ("does this
        # case carry this hint term?"), so those get a set for O(1) membership.
        # The set is bounded by the 13 hint rows, not by the whole vocabulary.
        hint_terms = {term for terms in self._hints.values() for term in terms}
        self._hint_postings = {
            term: set(self._postings[term])
            for term in hint_terms
            if term in self._postings
        }

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
        intent_bonus = self._intent_bonus(query_terms)
        family_bonus = self.calibration.get(self._query_family(query), {})
        # |query_terms & case_terms| for every path, in one pass over postings.
        # This is the same set intersection the previous implementation did per
        # path; the index just answers it without re-tokenising the case.
        matched: defaultdict[int, int] = defaultdict(int)
        for term in query_terms:
            for index in self._postings.get(term, ()):
                matched[index] += 1
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
            overlap = matched.get(index, 0)
            # A strategy hint contributes query terms the case text itself does
            # not carry, so only the ones this path is missing are added.
            for term in self._hints.get(strategy_key, ()):
                if term in query_terms and index not in self._hint_postings.get(term, ()):
                    overlap += 1
            if not overlap:
                continue
            score = (
                float(overlap)
                + intent_bonus.get(strategy_key, 0.0)
                + float(family_bonus.get(strategy_key, 0.0))
            )
            grouped[strategy["id"]].append(
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
        for strategy_id, rows in grouped.items():
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
            ranked.append(
                (
                    best_score := sum(item[0] for item in rows[:3]) / min(3, len(rows)),
                    BehaviorOption(
                        strategy=strategy.get("key", ""),
                        dialogue_act=best["action"].get("key", "wait"),
                        expression_pattern=expression.get("name", ""),
                        avoid=tuple(expression.get("avoid", [])[:6]),
                        media_intent=best["media"].get("key", "none"),
                        support_case_count=int(strategy.get("support_case_count", len(rows))),
                        examples=tuple(examples),
                        score=best_score,
                        snapshot_version=str(self.manifest.get("pipeline_version", "unknown")),
                    ),
                )
            )
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
