"""Label-layer analysis for the compiled behavior graph.

The offline benchmark established where the ceiling is: even with a *perfect*
situation classifier the graph's own situation→strategy statistics only reach
~41%. That number is a property of the **labels**, not of the retrieval — and it
is the reason further scorer tuning cannot pay off.

This script answers the follow-up questions that decide how to fix the labels:

* which strategies are ambiguous (co-occur inside the same situation family);
* how much of the ceiling is caused by one catch-all label (``supplement``);
* whether a different prediction target (the dialogue *act* rather than the
  strategy) is inherently less ambiguous;
* what the ceiling becomes under candidate label merges.

Read-only. Run from the repository root::

    .venv/bin/python scripts/analyze_behavior_labels.py
"""

from __future__ import annotations

import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qunbot.experience.graph_retrieval import BehaviorGraphRetriever

DEFAULT_GRAPH = Path("data/private/derived/behavior-graph-compiled-final-20261014-v8")


def entropy(counts: Counter) -> float:
    total = sum(counts.values()) or 1
    return -sum((n / total) * math.log2(n / total) for n in counts.values() if n)


def conditional_entropy(groups: dict[str, Counter]) -> float:
    """``H(label | family)`` — how much the family still leaves open."""
    total = sum(sum(c.values()) for c in groups.values()) or 1
    return sum(
        (sum(c.values()) / total) * entropy(c) for c in groups.values() if c
    )


def oracle_ceiling(groups: dict[str, Counter], *, drop: frozenset[str] = frozenset(),
                   merge: tuple[frozenset[str], ...] = ()) -> float:
    """Best accuracy reachable with perfect family knowledge.

    ``drop`` removes labels from the *predicted* set (a case whose truth is
    dropped is then always wrong). ``merge`` collapses groups of labels into one
    predicted class, which is how a coarser taxonomy is simulated.
    """
    alias: dict[str, str] = {}
    for group in merge:
        name = "|".join(sorted(group))
        for key in group:
            alias[key] = name
    hits = total = 0
    for counts in groups.values():
        pooled: Counter = Counter()
        for key, n in counts.items():
            total += n
            if key in drop:
                continue
            pooled[alias.get(key, key)] += n
        if pooled:
            hits += pooled.most_common(1)[0][1]
    return hits / (total or 1)


def pct(value: float) -> str:
    return f"{value * 100:5.1f}%"


def family_is_text_derived(paths) -> dict:
    """Verify every case's family is reproducible from its text alone.

    This is a regression guard, not a diagnostic. ``situation_family`` used to
    fall back to the case's own strategy when no keyword matched, which filed
    15% of cases under a family *because* of the label that family was then used
    to predict — a self-fulfilling statistic that inflated the measured ceiling
    by 1.7pt (13 classes) / 3.0pt (9 classes).

    The parameter is gone, so the honest check is that the compiled snapshot
    agrees with the text-only function for every case. If someone reintroduces a
    label-dependent family, the mismatch count jumps off zero immediately.
    """
    from scripts.compile_behavior_graph import situation_family

    mismatched: list[str] = []
    for case, _s, _a, _e, _m, situation, _ev in paths:
        expected, _ = situation_family(
            case.get("situation_text", ""), case.get("observable_cues", [])
        )
        if expected != situation.get("family_key"):
            mismatched.append(case.get("case_id", "?"))
    return {
        "cases": len(paths),
        "mismatched": len(mismatched),
        "examples": mismatched[:5],
    }


def main() -> int:
    retriever = BehaviorGraphRetriever(DEFAULT_GRAPH)
    paths = retriever.case_paths
    print(f"图谱路径: {len(paths)}\n")

    family_strategy: defaultdict[str, Counter] = defaultdict(Counter)
    family_act: defaultdict[str, Counter] = defaultdict(Counter)
    strategies: Counter = Counter()
    acts: Counter = Counter()
    for _case, strategy, action, _expression, _media, situation, _evidence in paths:
        s = strategy.get("key", "?")
        a = action.get("key", "?")
        f = situation.get("family_key") or "?"
        family_strategy[f][s] += 1
        family_act[f][a] += 1
        strategies[s] += 1
        acts[a] += 1

    total = sum(strategies.values()) or 1
    print("=== 策略分布（13 类）===")
    for key, n in strategies.most_common():
        print(f"  {key:16s} {n:5d}  {pct(n / total)}")
    print(f"  H(strategy) = {entropy(strategies):.2f} bit")

    print("\n=== 对话动作分布 ===")
    act_total = sum(acts.values()) or 1
    for key, n in acts.most_common():
        print(f"  {key:16s} {n:5d}  {pct(n / act_total)}")
    print(f"  H(act) = {entropy(acts):.2f} bit")

    print("\n=== 上限：情境完全正确时能答对多少 ===")
    base = oracle_ceiling(family_strategy)
    print(f"  全部 13 类               {pct(base)}   H(strategy|family) = "
          f"{conditional_entropy(family_strategy):.2f} bit")
    no_supp = oracle_ceiling(family_strategy, drop=frozenset({"supplement"}))
    print(f"  预测集合里去掉 supplement  {pct(no_supp)}   （supplement 占 {pct(strategies['supplement'] / total)}）")
    act_base = oracle_ceiling(family_act)
    print(f"  改预测「对话动作」        {pct(act_base)}   H(act|family) = "
          f"{conditional_entropy(family_act):.2f} bit")

    guard = family_is_text_derived(paths)
    print("\n=== 情境族是否只由文本决定（回归哨兵）===")
    print(f"  检查 {guard['cases']} 条案例，与纯文本函数不一致：{guard['mismatched']} 条")
    if guard["mismatched"]:
        print(f"  ⚠️ 不一致样例：{guard['examples']}")
        print("  ⇒ 有案例的情境族不是文本推出的，oracle 又被循环论证污染了。")
    else:
        print("  ⇒ 全部一致：情境族完全由文本关键词决定，oracle 是诚实的。")

    print("\n=== 每个情境族的歧义（前 3 策略）===")
    for family, counts in sorted(
        family_strategy.items(), key=lambda kv: -sum(kv[1].values())
    ):
        n = sum(counts.values())
        top = counts.most_common(3)
        share = top[0][1] / n
        spread = " ".join(f"{k}={v}" for k, v in top)
        print(f"  {family:28s} n={n:5d} 最高占比 {pct(share)}  {spread}")

    print("\n=== 合并方案模拟 ===")
    merges = {
        "supplement + acknowledge + agree（泛泛附和类）": (
            frozenset({"supplement", "acknowledge", "agree"}),
        ),
        "clarify + answer_directly（答疑类）": (
            frozenset({"clarify", "answer_directly"}),
        ),
        "上两条同时合并": (
            frozenset({"supplement", "acknowledge", "agree"}),
            frozenset({"clarify", "answer_directly"}),
        ),
        "empathize + encourage（情绪支持类）": (
            frozenset({"empathize", "encourage"}),
        ),
        "三类合并（附和 + 答疑 + 情绪支持）": (
            frozenset({"supplement", "acknowledge", "agree"}),
            frozenset({"clarify", "answer_directly"}),
            frozenset({"empathize", "encourage"}),
        ),
    }
    for name, merge in merges.items():
        ceiling = oracle_ceiling(family_strategy, merge=merge)
        classes = len(set(strategies) - {k for g in merge for k in g}) + len(merge)
        print(f"  {name:38s} 上限 {pct(ceiling)}  类别数 {classes}")

    print("\n=== 结论提示 ===")
    print("  · 上限由「同一情境下的策略是否可区分」决定，与检索打分无关。")
    print("  · supplement 在多数族里都是最高频——它是垃圾桶标签，不携带判别信息。")
    print("  · 若某个合并把上限抬不动，说明该组内部本来就不矛盾，合并无收益。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
