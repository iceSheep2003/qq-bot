"""Validate the private behavior-validation package before calibration.

This is a data-quality gate, not a model judge.  It checks the invariants that
make the review set safe to use as held-out evidence:

* the marked current message is present exactly once and is the last message;
* packets do not contain obvious account identifiers or contact details;
* every accepted label has a sufficiently confident DeepSeek review;
* the merged manifest agrees with the actual rows on disk.

It deliberately does not score whether a behavior label is socially correct;
that requires human review or replay.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


IDENTIFIER_PATTERNS = (
    re.compile(r"(?<!\d)\d{5,18}[Xx]?(?!\d)"),
    re.compile(r"(?i)\b(?:user|group|uin|qq)[_-]?[a-z0-9_-]{6,}\b"),
    re.compile(r"https?://\S+|www\.\S+", re.I),
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
)


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def packet_errors(packet: dict) -> list[str]:
    errors: list[str] = []
    context = packet.get("context")
    if not isinstance(context, list) or not context:
        return ["empty_context"]
    current = [row for row in context if row.get("is_current")]
    current_id = packet.get("current_message_id")
    if len(current) != 1:
        errors.append("current_count")
    elif current[0].get("id") != current_id:
        errors.append("current_id_mismatch")
    if context[-1].get("id") != current_id:
        errors.append("future_message_or_wrong_order")
    for row in context:
        text = str(row.get("text") or "")
        if any(pattern.search(text) for pattern in IDENTIFIER_PATTERNS):
            errors.append("identifier_or_contact_leak")
            break
    return errors


def validate(reviewed: Path, final: Path, *, threshold: float = 0.65) -> dict:
    packets = read_jsonl(reviewed / "packets.jsonl")
    drafts = read_jsonl(reviewed / "assistant_draft.jsonl")
    reviews = read_jsonl(reviewed / "deepseek_reviews.jsonl")
    merged = read_jsonl(final / "validated.jsonl")
    manifest = json.loads((final / "manifest.json").read_text(encoding="utf-8"))

    errors: list[dict] = []
    for packet in packets:
        for code in packet_errors(packet):
            errors.append({"sample_id": packet.get("sample_id"), "code": code})

    packet_ids = {row.get("sample_id") for row in packets}
    draft_ids = {row.get("sample_id") for row in drafts}
    review_rows = [row.get("review", {}) for row in reviews]
    review_ids = {row.get("sample_id") for row in review_rows}
    merged_ids = {row.get("sample_id") for row in merged}
    if packet_ids != draft_ids:
        errors.append({"code": "draft_packet_set_mismatch"})
    if packet_ids != merged_ids:
        errors.append({"code": "merged_packet_set_mismatch"})
    if len(review_ids) != len(review_rows):
        errors.append({"code": "duplicate_review_sample_id"})
    if not set(review_ids) <= packet_ids:
        errors.append({"code": "review_for_unknown_sample"})

    expected_accepted = 0
    for row in merged:
        review = row.get("deepseek_review") or {}
        label = row.get("validated_label")
        eligible = (
            review.get("verdict") in {"accept", "correct"}
            and float(review.get("confidence", 0)) >= threshold
        )
        if eligible:
            expected_accepted += 1
        if bool(label) != eligible:
            errors.append({"sample_id": row.get("sample_id"), "code": "label_confidence_mismatch"})

    actual_status = Counter(row.get("status") for row in merged)
    declared_status = Counter(manifest.get("status_counts", {}))
    if dict(actual_status) != dict(declared_status):
        errors.append({"code": "manifest_status_mismatch"})
    if manifest.get("accepted_for_calibration") != expected_accepted:
        errors.append({"code": "manifest_accepted_count_mismatch"})
    report = {
        "schema_version": 1,
        "packets": len(packets),
        "drafts": len(drafts),
        "deepseek_reviews": len(reviews),
        "merged": len(merged),
        "accepted_for_calibration": expected_accepted,
        "review_confidence_threshold": threshold,
        "status_counts": dict(actual_status),
        "errors": errors,
        "status": "pass" if not errors else "fail",
        "note": "quality gate only; semantic correctness still needs human review or replay",
    }
    if errors:
        raise ValueError(json.dumps(report, ensure_ascii=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed", type=Path, required=True)
    parser.add_argument("--final", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=0.65)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate(args.reviewed, args.final, threshold=args.threshold)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
