"""Evaluate and publish a conservative behavior-graph snapshot.

This is a local deterministic gate.  It does not claim that a model's
``outcome`` field proves success; it only verifies provenance, schema and
privacy invariants before a candidate can be offered to retrieval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from scripts.compile_behavior_graph import situation_family

GRAPH_PIPELINE_VERSION = "behavior-graph-compile-v4"


STRATEGIES = {
    "answer_directly", "acknowledge", "agree", "empathize", "banter",
    "tease_gently", "supplement", "clarify", "encourage", "deescalate",
    "topic_shift", "celebrate", "abstain",
}
ACTIONS = {
    "answer", "acknowledge", "agree", "empathize", "joke", "tease",
    "add_information", "clarify", "encourage", "calm", "bridge_topic",
    "celebrate", "wait",
}
MEDIA = {"none", "meme", "voice", "text_plus_meme", "text_plus_voice", "native_reaction"}
PII = (
    re.compile(r"user_[A-Za-z0-9_-]{6,}"),
    re.compile(r"group_[A-Za-z0-9_-]{6,}"),
    re.compile(r"https?://|www\.", re.I),
    re.compile(r"(?<!\d)\d{8,16}(?!\d)"),
    re.compile(r"(?:手机号|QQ号|微信|vx|wechat)", re.I),
)
UNSAFE_EXPRESSION = (
    re.compile(r"一连串.{0,12}问题"),
    re.compile(r"连续(?:地)?追问|反复追问|连续提问"),
    re.compile(r"不断(?:地)?提问|每次都提问"),
)


def jsonl(path: Path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


def text_fields(row: dict) -> str:
    fields = ("situation", "expression_pattern", "abstraction", "topic_terms", "avoid")
    return " ".join(str(row.get(field, "")) for field in fields)


def validate(row: dict, valid_cases: set[str], message_ids: set[str]) -> list[str]:
    reasons = []
    if row.get("case_id") not in valid_cases:
        reasons.append("unknown_case")
    refs = list(dict.fromkeys(row.get("source_message_ids", [])))
    if len(refs) < 2:
        reasons.append("insufficient_evidence")
    if len(refs) > 12:
        reasons.append("evidence_budget_exceeded")
    if not set(refs) <= message_ids:
        reasons.append("unknown_message_reference")
    required = set(row.get("required_message_ids", []))
    if not required <= set(refs):
        reasons.append("required_reply_evidence_missing")
    if row.get("strategy") not in STRATEGIES:
        reasons.append("unknown_strategy")
    if row.get("dialogue_act") not in ACTIONS:
        reasons.append("unknown_dialogue_act")
    if row.get("media_intent") not in MEDIA:
        reasons.append("unknown_media_intent")
    if not str(row.get("situation", "")).strip():
        reasons.append("empty_situation")
    if not str(row.get("expression_pattern", "")).strip():
        reasons.append("empty_expression")
    if not str(row.get("abstraction", "")).strip():
        reasons.append("empty_abstraction")
    if any(pattern.search(text_fields(row)) for pattern in PII):
        reasons.append("sensitive_text")
    if any(pattern.search(str(row.get("expression_pattern", ""))) for pattern in UNSAFE_EXPRESSION):
        reasons.append("unsafe_question_pattern")
    if row.get("media_intent") == "text_plus_voice" and len(str(row.get("expression_pattern", ""))) > 240:
        reasons.append("voice_intent_without_bounded_expression")
    return sorted(set(reasons))


def evaluate(source: Path, corpus: Path, output: Path, min_support: int) -> dict:
    proposals = jsonl(source / "behavior_proposals.jsonl")
    candidates = {
        row["id"]: row
        for row in jsonl(corpus / "training_candidates.jsonl")
        if row.get("split") == "train"
    }
    messages = {row["id"] for row in jsonl(corpus / "messages.jsonl")}
    reasons = Counter()
    valid_rows = []
    rejected_rows = []
    seen = set()
    for row in proposals:
        case_id = row.get("case_id")
        if case_id in seen:
            reasons["duplicate_case"] += 1
            rejected_rows.append({"case_id": case_id, "reasons": ["duplicate_case"]})
            continue
        seen.add(case_id)
        row_reasons = validate(row, set(candidates), messages)
        if row_reasons:
            reasons.update(row_reasons)
            rejected_rows.append({"case_id": case_id, "reasons": row_reasons})
        else:
            valid_rows.append(row)

    by_strategy: defaultdict[str, list[dict]] = defaultdict(list)
    for row in valid_rows:
        by_strategy[row["strategy"]].append(row)
    published = []
    for strategy, rows in sorted(by_strategy.items()):
        if len(rows) < min_support:
            reasons["strategy_below_support_threshold"] += len(rows)
            continue
        # Keep representative cases bounded.  Evidence remains in the full
        # graph; retrieval only receives a few diverse examples.
        representatives = []
        seen_situations = set()
        for row in sorted(rows, key=lambda item: item["case_id"]):
            situation = row["situation"].strip().lower()
            if situation in seen_situations:
                continue
            seen_situations.add(situation)
            family_key, family_label = situation_family(
                row["situation"], row.get("observable_cues", []), strategy
            )
            representatives.append(
                {
                    "case_id": row["case_id"],
                    "situation_family": family_key,
                    "situation_family_label": family_label,
                    "situation": row["situation"],
                    "dialogue_act": row["dialogue_act"],
                    "expression_pattern": row["expression_pattern"],
                    "avoid": row.get("avoid", []),
                    "media_intent": row["media_intent"],
                    "outcome": "observed",
                }
            )
            if len(representatives) >= 5:
                break
        published.append(
            {
                "strategy": strategy,
                "status": "eligible_candidate",
                "support_case_count": len(rows),
                "representatives": representatives,
                "rule": "model proposal with complete evidence; outcome requires replay evaluation",
            }
        )

    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    write_jsonl(output / "published_options.jsonl", published)
    write_jsonl(output / "quarantine.jsonl", rejected_rows)
    manifest = {
        "schema_version": 1,
        "pipeline_version": "behavior-graph-evaluation-v1",
        "input_proposals": len(proposals),
        "valid_proposals": len(valid_rows),
        "quarantined_proposals": len(rejected_rows),
        "published_strategy_options": len(published),
        "min_support": min_support,
        "reasons": dict(reasons),
        "privacy": "private_deidentified_evidence_only_no_person_nodes",
        "status": "candidate_snapshot",
        "graph_pipeline_version": GRAPH_PIPELINE_VERSION,
    }
    temp = output / "manifest.json.tmp"
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(output / "manifest.json")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-support", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.source, args.corpus, args.output, max(1, args.min_support)), ensure_ascii=False))


if __name__ == "__main__":
    main()
