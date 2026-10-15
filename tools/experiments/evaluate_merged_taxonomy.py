"""Re-evaluate the retrieval under candidate merged taxonomies.

``analyze_behavior_labels.py`` shows the *oracle* ceiling rises when ambiguous
strategies are merged. That is necessary but not sufficient: the ceiling could
rise while the retrieval gains nothing, because merging only helps if the
classes being merged are exactly the ones the retriever confuses.

This script runs the same 5-fold session cross-validation as the main benchmark,
but maps both the prediction and the ground truth through a merge table before
scoring. The shipped default scorer is used (interactional fields + IDF, no
hand-written boosts), so the comparison isolates the taxonomy.

Read-only. Run from the repository root::

    .venv/bin/python -m tools.experiments.evaluate_merged_taxonomy
"""

from __future__ import annotations

import sys
from pathlib import Path


from qunbot.agent.cognition.experience.graph_retrieval import DEFAULT_TAXONOMY  # noqa: E402
from tools.experiments.bench_behavior_retrieval import (  # noqa: E402
    DEFAULT_CALIBRATION,
    DEFAULT_CORPUS,
    DEFAULT_FOLDS,
    DEFAULT_GRAPH,
    build_idf,
    build_postings,
    fold_of,
    interaction_text,
    prepare,
    query_for,
    rank,
    score_run,
)

#: Candidate relabellings. A label absent from the map keeps its own name.
#: The last row is the shipped default, imported rather than retyped so the
#: measurement and the code can never drift apart.
MERGES: dict[str, dict[str, str]] = {
    "原样（13 类）": {},
    "合并「泛泛附和」": {
        "supplement": "附和",
        "acknowledge": "附和",
        "agree": "附和",
    },
    "合并「答疑」": {"clarify": "答疑", "answer_directly": "答疑"},
    "合并「情绪支持」": {"empathize": "情绪支持", "encourage": "情绪支持"},
    "三类都合并（9 类）": {
        "supplement": "附和",
        "acknowledge": "附和",
        "agree": "附和",
        "clarify": "答疑",
        "answer_directly": "答疑",
        "empathize": "情绪支持",
        "encourage": "情绪支持",
    },
    "只合并「答疑」+「情绪支持」（11 类）": {
        "clarify": "答疑",
        "answer_directly": "答疑",
        "empathize": "情绪支持",
        "encourage": "情绪支持",
    },
    "线上默认 DEFAULT_TAXONOMY": DEFAULT_TAXONOMY,
}


def evaluate(retriever, messages, corpus_cases, merge: dict[str, str], folds: int) -> dict:
    """Five-fold session CV, with labels mapped through ``merge``."""
    session_of = {cid: row.get("session_id") for cid, row in corpus_cases.items()}
    truth_of = {p[0].get("case_id"): p[1].get("key", "") for p in retriever.case_paths}
    queries = {
        cid: query_for(case, messages)
        for cid, case in corpus_cases.items()
        if cid in truth_of
    }
    saved = retriever.case_paths
    fold_of_case = {
        p[0].get("case_id"): fold_of(session_of.get(p[0].get("case_id"), ""), folds)
        for p in saved
    }

    def mapped(key: str) -> str:
        return merge.get(key, key)

    rows: list[dict] = []
    for fold in range(folds):
        held = [p for p in saved if fold_of_case.get(p[0].get("case_id")) == fold]
        if not held:
            continue
        retriever.case_paths = [
            p for p in saved if fold_of_case.get(p[0].get("case_id")) != fold
        ]
        retriever._build_index()
        postings, hints = build_postings(retriever, interaction_text)
        idf = build_idf(retriever, postings)
        for path in held:
            case_id = path[0].get("case_id")
            query = queries.get(case_id)
            if query is None:
                continue
            options = rank(
                retriever,
                query,
                postings=postings,
                hint_postings=hints,
                idf=idf,
                use_hints=False,
                use_intent=False,
                use_family=False,
            )
            rows.append(
                {
                    "case_id": case_id,
                    "truth": mapped(truth_of.get(case_id, "")),
                    "strategies": [mapped(option["strategy"]) for option in options],
                }
            )

    retriever.case_paths = saved
    retriever._build_index()
    return score_run(rows, len(rows))


def main() -> int:
    retriever, messages, corpus_cases = prepare(
        DEFAULT_CORPUS, DEFAULT_GRAPH, DEFAULT_CALIBRATION
    )
    print(f"图谱 {len(retriever.case_paths)} 路径 · {DEFAULT_FOLDS} 折会话交叉验证\n")
    print(f"{'方案':34s} {'类别':>4s} {'Top-1':>7s} {'平衡准确率':>10s} {'macro-F1':>9s} {'任意@3':>8s}")
    baseline: float | None = None
    for name, merge in MERGES.items():
        result = evaluate(retriever, messages, corpus_cases, merge, DEFAULT_FOLDS)
        classes = len({merge.get(k, k) for k in result["per_class"]})
        top1 = result["top1_accuracy"]
        if baseline is None:
            baseline = top1
        delta = "" if baseline is None else f"  ({top1 - baseline:+.3f})"
        print(
            f"{name:34s} {classes:4d} {top1 * 100:6.1f}% "
            f"{result['balanced_accuracy'] * 100:9.1f}% "
            f"{result['macro_f1'] * 100:8.1f}% "
            f"{result['any_at_3'] * 100:7.1f}%{delta}"
        )
    print("\n判读：Top-1 抬升幅度接近 oracle 上限抬升幅度（+8.3pt），说明检索的混淆")
    print("      正好落在被合并的类之间——合并是有效的；若几乎不动，说明混淆在别处。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
