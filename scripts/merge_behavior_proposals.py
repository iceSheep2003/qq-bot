"""Merge resumable behavior-extraction shards with coverage checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def write_jsonl(path: Path, rows: list[dict]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    args = parser.parse_args()
    expected = [
        json.loads(line)["id"]
        for line in (args.corpus / "training_candidates.jsonl").open(encoding="utf-8")
        if json.loads(line).get("split") == "train"
    ]
    expected_set = set(expected)
    candidate_index = {
        row["id"]: row
        for row in (
            json.loads(line)
            for line in (args.corpus / "training_candidates.jsonl").open(encoding="utf-8")
        )
        if row.get("split") == "train"
    }
    selected_rows: dict[str, dict] = {}
    duplicate_count = 0
    source_counts = {}
    for source in args.source:
        path = source / "behavior_proposals.jsonl"
        rows = [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]
        source_counts[str(source)] = len(rows)
        for row in rows:
            case_id = row["case_id"]
            if case_id in selected_rows:
                duplicate_count += 1
                continue
            if case_id not in expected_set:
                raise SystemExit(f"proposal case is outside training corpus: {case_id}")
            case = candidate_index[case_id]
            selected_refs = list(dict.fromkeys(row.get("source_message_ids", [])))
            required = [case.get("reply_target_id"), case.get("continuation_id")]
            required = [message_id for message_id in required if message_id]
            # The model may omit either endpoint while abstracting.  The
            # graph's evidence contract cannot: retain the model selection,
            # then append the explicit target and actual continuation.
            row["selected_source_message_ids"] = selected_refs
            row["required_message_ids"] = required
            row["source_message_ids"] = list(dict.fromkeys(selected_refs + required))
            row.setdefault("evidence", {})["source_message_ids"] = row["source_message_ids"]
            row["evidence"]["required_message_ids"] = required
            selected_rows[case_id] = row
    ordered = [selected_rows[case_id] for case_id in expected if case_id in selected_rows]
    missing = [case_id for case_id in expected if case_id not in selected_rows]
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.chmod(0o700)
    write_jsonl(args.output / "behavior_proposals.jsonl", ordered)
    failed = []
    for source in args.source:
        path = source / "failed_cases.jsonl"
        if path.exists():
            failed.extend(json.loads(line) for line in path.open(encoding="utf-8") if line.strip())
    write_jsonl(args.output / "failed_cases.jsonl", failed)
    report = {
        "schema_version": 1,
        "expected_train_cases": len(expected),
        "proposals": len(ordered),
        "missing_cases": len(missing),
        "missing_case_ids": missing,
        "duplicates_removed": duplicate_count,
        "failed_cases": len(failed),
        "source_counts": source_counts,
        "status": "merged_with_coverage_report",
    }
    temp = args.output / "manifest.json.tmp"
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(args.output / "manifest.json")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
