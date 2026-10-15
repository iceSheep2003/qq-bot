"""一次性等价性验证：比对 graph_retrieval 优化前后 retrieve() 的输出。

改动前：每次调用重建 2007 条检索串并重新分词。
改动后：__init__ 建一次倒排索引。
两者必须在 策略/动作/表达结构/分数/避免项/媒体倾向/支撑案例数/证据案例 上逐字一致。
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qunbot.experience.graph_retrieval import BehaviorGraphRetriever
from qunbot.experience.retrieval import BehaviorOption, _HINTS, _terms

GRAPH = Path("data/private/derived/behavior-graph-compiled-final-20261014-v7")
CORPUS = Path("data/private/derived/behavior-corpus-20261014")
CAL = Path("data/private/derived/behavior-validation-final-20261014/calibration.json")


def old_retrieve(self, query, *, limit=3):
    """优化前 retrieve() 的逐字复刻，作为参考实现。"""
    if not isinstance(query, str) or not query.strip():
        return []
    limit = max(1, min(3, int(limit)))
    query_terms = _terms(query)
    intent_bonus = self._intent_bonus(query_terms)
    family_bonus = self.calibration.get(self._query_family(query), {})
    grouped = defaultdict(list)
    for case, strategy, action, expression, media, situation, evidence_id in self._paths():
        searchable = " ".join(
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
        overlap = query_terms & _terms(searchable)
        overlap |= query_terms & self._hints.get(strategy.get("key", ""), set())
        if not overlap:
            continue
        sk = strategy.get("key", "")
        score = (
            float(len(overlap))
            + intent_bonus.get(sk, 0.0)
            + float(family_bonus.get(sk, 0.0))
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
    for _sid, rows in grouped.items():
        rows.sort(key=lambda it: (-it[0], it[1]["case"].get("case_id", "")))
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
        best_score = sum(i[0] for i in rows[:3]) / min(3, len(rows))
        ranked.append(
            (
                best_score,
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
    ranked.sort(key=lambda it: (-it[0], -it[1].support_case_count, it[1].strategy))
    return [it[1] for it in ranked[:limit]]


def signature(options):
    return [
        (
            o.strategy,
            o.dialogue_act,
            o.expression_pattern,
            round(o.score, 9),
            tuple(o.avoid),
            o.media_intent,
            o.support_case_count,
            tuple(sorted(str(e.get("case_id") or "") for e in o.examples)),
            tuple(sorted(str(e.get("evidence_id") or "") for e in o.examples)),
        )
        for o in options
    ]


def load_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def main() -> int:
    retriever = BehaviorGraphRetriever(GRAPH, calibration=CAL)
    messages = {row["id"]: row for row in load_jsonl(CORPUS / "messages.jsonl")}

    def query_for(case, window=10):
        rows = [messages[m] for m in case["context_ids"][-window:] if m in messages]
        return "\n".join(str(row.get("text", ""))[:500] for row in rows)

    candidates = load_jsonl(CORPUS / "training_candidates.jsonl")
    cases = [c for c in candidates if c.get("split") in ("validation", "test")]
    print(f"用例总数: {len(cases)}")

    mismatches = []
    old_total = new_total = 0.0
    for case in cases:
        query = query_for(case)
        start = time.perf_counter()
        before = old_retrieve(retriever, query, limit=3)
        old_total += time.perf_counter() - start
        start = time.perf_counter()
        after = retriever.retrieve(query, limit=3)
        new_total += time.perf_counter() - start
        if signature(before) != signature(after):
            mismatches.append((case["id"], before, after))

    print(f"不一致用例: {len(mismatches)}")
    for case_id, before, after in mismatches[:5]:
        print(f"  MISMATCH {case_id}")
        print(f"    old: {[(o.strategy, round(o.score, 4)) for o in before]}")
        print(f"    new: {[(o.strategy, round(o.score, 4)) for o in after]}")

    n = len(cases) or 1
    print(f"旧实现: {old_total:.1f}s  平均 {old_total / n * 1000:.1f} ms/次")
    print(f"新实现: {new_total:.1f}s  平均 {new_total / n * 1000:.2f} ms/次")
    print(f"提速: {old_total / new_total:.0f}x")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
