"""Offline held-out evaluation for behavior-graph retrieval.

The graph is built from train cases only.  Validation/test cases are used only
as queries, never as graph evidence.  The family-alignment metric is an
explicit heuristic proxy, not a human-quality score; human review and replay
remain necessary before online activation.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from qunbot.experience.graph_retrieval import BehaviorGraphRetriever
from qunbot.experience.retrieval import BehaviorRetriever, _terms
from scripts.compile_behavior_graph import situation_family


def jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def query_for(case: dict, messages: dict[str, dict], window: int) -> str:
    rows = [messages[mid] for mid in case["context_ids"][-window:] if mid in messages]
    return "\n".join(str(row.get("text", ""))[:500] for row in rows)


def evaluate(corpus: Path, graph: Path, flat: Path, split: str, limit: int = 0, calibration: Path | None = None) -> dict:
    messages = {row["id"]: row for row in jsonl(corpus / "messages.jsonl")}
    cases = [row for row in jsonl(corpus / "training_candidates.jsonl") if row.get("split") == split]
    if limit > 0:
        cases = cases[:limit]
    graph_retriever = BehaviorGraphRetriever(graph, calibration=calibration)
    flat_retriever = BehaviorRetriever(flat)
    results = {}
    for name, retriever in (("graph", graph_retriever), ("flat", flat_retriever)):
        covered = top1_family = any_family = leaked = 0
        strategy_counts = Counter()
        topic_overlap = []
        details = []
        for case in cases:
            query = query_for(case, messages, 10)
            options = retriever.retrieve(query, limit=3)
            if options:
                covered += 1
                strategy_counts[options[0].strategy] += 1
            expected_family, _ = situation_family(query, [])
            families = {
                example.get("situation_family")
                for option in options
                for example in option.examples
                if example.get("situation_family")
            }
            if options and options[0].examples and options[0].examples[0].get("situation_family") == expected_family:
                top1_family += 1
            if expected_family in families:
                any_family += 1
            query_terms = _terms(query)
            matched_topics = {
                topic
                for option in options
                for example in option.examples
                for topic in example.get("topic_terms", [])
                if _terms(topic) & query_terms
            }
            topic_overlap.append(bool(matched_topics))
            source_ids = set(case["context_ids"] + [case["continuation_id"]])
            evidence_ids = {
                example.get("evidence_id")
                for option in options
                for example in option.examples
                if example.get("evidence_id")
            }
            if source_ids & evidence_ids:
                leaked += 1
            if len(details) < 20:
                details.append({"case_id": case["id"], "expected_family": expected_family, "strategies": [o.strategy for o in options]})
        total = len(cases) or 1
        results[name] = {
            "cases": len(cases),
            "coverage_at_3": round(covered / total, 4),
            "family_alignment_top1": round(top1_family / total, 4),
            "family_alignment_any_at_3": round(any_family / total, 4),
            "topic_overlap_any_at_3": round(sum(topic_overlap) / total, 4),
            "evidence_leakage_cases": leaked,
            "top1_strategy_counts": dict(strategy_counts),
            "sample_details": details,
        }
    return {
        "schema_version": 1,
        "evaluation": "held_out_retrieval_proxy",
        "train_graph": str(graph),
        "flat_snapshot": str(flat),
        "calibration": str(calibration) if calibration else None,
        "split": split,
        "note": "family alignment is a deterministic proxy, not a human quality score",
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--flat", type=Path, required=True)
    parser.add_argument("--split", choices=("validation", "test"), default="test")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calibration", type=Path)
    args = parser.parse_args()
    report = evaluate(args.corpus, args.graph, args.flat, args.split, args.limit, args.calibration)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
