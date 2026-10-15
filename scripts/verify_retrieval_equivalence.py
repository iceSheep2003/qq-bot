"""一次性等价性验证：比对 graph_retrieval 的倒排索引实现与朴素逐条扫描。

``BehaviorGraphRetriever`` 用倒排索引回答「每条案例命中了查询里的哪些词」。
索引只是把分词从「每次查询重算」提前到「加载时算一次」，它不允许改变任何一条
案例的得分或排序。本脚本用一份朴素实现（每条案例现算分词与 IDF 加权）复刻
当前打分，逐案例比对 ``retrieve()`` 的输出：策略/动作/表达结构/分数/避免项/
媒体倾向/支撑案例数/证据案例 必须逐字一致。

打分本身（互动线索字段 + IDF 加权 + 关闭手写加分）是被离线评测选中的默认值，
不是本脚本要验证的东西；本脚本只验证「索引 == 朴素扫描」。
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qunbot.experience.graph_retrieval import BehaviorGraphRetriever
from qunbot.experience.retrieval import BehaviorOption, _terms

GRAPH = Path("data/private/derived/behavior-graph-compiled-final-20261014-v8")
CORPUS = Path("data/private/derived/behavior-corpus-20261014")
CAL = Path("data/private/derived/behavior-validation-final-20261014/calibration.json")


def naive_retrieve(self, query, *, limit=3):
    """朴素参考实现：每条案例现算分词、现算 IDF 加权，不借助倒排索引。

    它复刻 ``retrieve()`` 当前的打分（互动线索字段 + IDF + 无手写加分），
    并同样应用先验校准（若启用）。这是「索引没有改变语义」的对照基准。
    """
    if not isinstance(query, str) or not query.strip():
        return []
    limit = max(1, min(3, int(limit)))
    query_terms = _terms(query)
    total_paths = len(self.case_paths) or 1
    intent_bonus = self._intent_bonus(query_terms) if self.intent_bonus else {}
    family_bonus = (
        self.calibration.get(self._query_family(query), {}) if self.family_bonus else {}
    )
    grouped = defaultdict(list)
    for case, strategy, action, expression, media, situation, evidence_id in self._paths():
        terms = query_terms & _terms(self._searchable(
            (case, strategy, action, expression, media, situation, evidence_id)
        ))
        if self.hint_union:
            terms |= query_terms & self._hints.get(strategy.get("key", ""), set())
        if not terms:
            continue
        if self.use_idf:
            score = sum(
                math.log(total_paths / (len(self._postings.get(term, ())) or 1))
                for term in terms
            )
        else:
            score = float(len(terms))
        key = strategy.get("key", "")
        score += intent_bonus.get(key, 0.0) + float(family_bonus.get(key, 0.0))
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
    if self.prior_weight is not None:
        ceiling = max((it[0] for it in ranked), default=0.0) or 1.0
        ranked = [
            (
                self.prior_weight * (score / ceiling)
                + math.log(self._prior.get(option.strategy, 1e-6)),
                option,
            )
            for score, option in ranked
        ]
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
    # ``taxonomy=None`` on purpose. This script exists to prove one thing: that
    # replacing the per-query re-tokenisation with an inverted index changed no
    # output. The coarse taxonomy added later *is* a deliberate behaviour change
    # (it re-groups the options), so it is switched off here; otherwise the check
    # would fail for the wrong reason and stop being evidence of anything.
    retriever = BehaviorGraphRetriever(GRAPH, calibration=CAL, taxonomy=None)
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
        before = naive_retrieve(retriever, query, limit=3)
        old_total += time.perf_counter() - start
        start = time.perf_counter()
        after = retriever.retrieve(query, limit=3)
        new_total += time.perf_counter() - start
        if signature(before) != signature(after):
            mismatches.append((case["id"], before, after))

    print(f"不一致用例: {len(mismatches)}")
    for case_id, before, after in mismatches[:5]:
        print(f"  MISMATCH {case_id}")
        print(f"    naive: {[(o.strategy, round(o.score, 4)) for o in before]}")
        print(f"    index: {[(o.strategy, round(o.score, 4)) for o in after]}")

    n = len(cases) or 1
    print(f"朴素扫描: {old_total:.1f}s  平均 {old_total / n * 1000:.1f} ms/次")
    print(f"倒排索引: {new_total:.1f}s  平均 {new_total / n * 1000:.2f} ms/次")
    if new_total:
        print(f"提速: {old_total / new_total:.0f}x")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
