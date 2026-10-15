"""Measure how consistent the extracted behavior labels are.

The question this answers is not "is the retrieval good" but "is the *corpus*
good". Those are different failures with different fixes: a retrieval problem is
fixed by better scoring, a corpus problem only by re-extracting. Before spending
another extraction run, it is worth knowing which one we have.

The test: if two cases describe the same situation, they should carry the same
strategy. So find the near-duplicate pairs and count how often the labels agree.

Read-only. Run from the repository root::

    .venv/bin/python -m tools.experiments.measure_label_consistency
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path


from qunbot.agent.cognition.experience.graph_retrieval import BehaviorGraphRetriever
from qunbot.agent.cognition.experience.retrieval import _terms

DEFAULT_GRAPH = Path("data/private/derived/behavior-graph-compiled-final-20261014-v8")

#: Jaccard thresholds. 0.6 is "the descriptions are nearly the same wording";
#: 0.3 is "clearly about the same kind of moment".
NEAR_DUPLICATE = 0.6
RELATED = 0.3


def main() -> int:
    retriever = BehaviorGraphRetriever(DEFAULT_GRAPH)
    rows = []
    for case, strategy, _a, _e, _m, _s, _ev in retriever.case_paths:
        # Only the parts of the extraction that are *not* the label: what the
        # situation was, and what was observable in it.
        text = case.get("situation_text", "") + " " + " ".join(
            case.get("observable_cues", [])
        )
        rows.append(
            {
                "case_id": case.get("case_id", "?"),
                "terms": frozenset(_terms(text)),
                "strategy": strategy.get("key", ""),
                "situation": case.get("situation_text", ""),
                "cues": case.get("observable_cues", []),
            }
        )
    total = len(rows)
    print(f"案例数: {total}\n")

    # Chance agreement: the probability two cases drawn from this label
    # distribution happen to share a class. Comparing against the majority-class
    # rate would be wrong — that answers a different question.
    share = Counter(row["strategy"] for row in rows)
    chance = sum((n / total) ** 2 for n in share.values())
    print(f"标签分布（{len(share)} 类）")
    for key, n in share.most_common(6):
        print(f"  {key:16s} {n:5d}  {n / total * 100:5.1f}%")
    print(f"  …")
    print(f"随机一致率（两案例碰巧同类）: {chance * 100:.1f}%\n")

    postings: defaultdict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        for term in row["terms"]:
            postings[term].append(index)

    def jaccard(a: frozenset, b: frozenset) -> float:
        return len(a & b) / len(a | b) if a and b else 0.0

    buckets = {
        "近似重复": (NEAR_DUPLICATE, 1.01),
        "明显相关": (RELATED, NEAR_DUPLICATE),
    }
    tally = {name: {"n": 0, "agree": 0, "pairs": []} for name in buckets}
    for index, row in enumerate(rows):
        shared_with: Counter = Counter()
        for term in row["terms"]:
            for other in postings[term]:
                if other > index:
                    shared_with[other] += 1
        for other, shared in shared_with.most_common(40):
            if shared < 4:
                break
            value = jaccard(row["terms"], rows[other]["terms"])
            for name, (low, high) in buckets.items():
                if low <= value < high:
                    entry = tally[name]
                    entry["n"] += 1
                    if row["strategy"] == rows[other]["strategy"]:
                        entry["agree"] += 1
                    elif name == "近似重复":
                        entry["pairs"].append((value, index, other))
                    break

    print("一致率（同一情境是否得到同一标签）")
    for name, entry in tally.items():
        if not entry["n"]:
            continue
        rate = entry["agree"] / entry["n"]
        print(
            f"  {name}: {rate * 100:5.1f}%   n={entry['n']}  "
            f"（随机基线 {chance * 100:.1f}%，倍数 {rate / chance:.1f}x）"
        )

    disagree = sorted(tally["近似重复"]["pairs"], reverse=True)
    print(f"\n近似重复但标签不一致：{len(disagree)} 对")
    for value, i, j in disagree[:5]:
        a, b = rows[i], rows[j]
        print(f"\n--- Jaccard={value:.2f} | {a['strategy']} vs {b['strategy']} ---")
        print(f"  A {a['situation'][:76]}")
        print(f"  B {b['situation'][:76]}")

    print(
        "\n判读：近似重复的一致率若远低于 100%，说明抽取本身不稳定 —— "
        "那是语料问题，只能靠重新抽取（配合更严的标签定义与一致性校验）解决，"
        "继续调检索权重不会有任何帮助。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
