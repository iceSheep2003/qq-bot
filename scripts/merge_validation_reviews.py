"""Merge assistant drafts and DeepSeek reviews without inventing missing labels."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path


def jsonl(path: Path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path: Path, rows):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        os.chmod(tmp, 0o600)
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    drafts = {row["sample_id"]: row for row in jsonl(args.draft)}
    reviews = {}
    for row in jsonl(args.reviews):
        review = row["review"]
        reviews.setdefault(review["sample_id"], review)
    merged = []
    status_counts = Counter()
    for sample_id, draft in drafts.items():
        review = reviews.get(sample_id)
        if review is None:
            status = "missing_review"
            label = None
        elif review["confidence"] < 0.65:
            status = "disputed_low_confidence"
            label = None
        elif review["verdict"] == "reject":
            status = "rejected"
            label = None
        else:
            status = "validated" if review["verdict"] == "accept" else "validated_corrected"
            label = {
                "situation_family": review["situation_family"],
                "response_mode": review["response_mode"],
                "strategy": review["strategy"],
                "dialogue_act": review["dialogue_act"],
                "media_intent": review["media_intent"],
                "avoid": review["avoid"],
                "confidence": review["confidence"],
            }
        status_counts[status] += 1
        merged.append({
            "sample_id": sample_id,
            "case_id": draft["case_id"],
            "packet_sha256": draft["packet_sha256"],
            "context": draft["context"],
            "assistant_draft": draft["assistant_draft"],
            "deepseek_review": review,
            "status": status,
            "validated_label": label,
        })
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_jsonl(args.output / "validated.jsonl", merged)
    manifest = {
        "schema_version": 1,
        "source": "validation-only",
        "cases": len(merged),
        "reviewed": len(reviews),
        "status_counts": dict(status_counts),
        "accepted_for_calibration": status_counts["validated"] + status_counts["validated_corrected"],
        "low_confidence_threshold": 0.65,
        "note": "model-reviewed labels, not human gold labels",
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(args.output / "manifest.json", 0o600)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
