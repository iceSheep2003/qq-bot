"""Prepare private held-out annotation packets; never invent speakers or labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from tools.experiments.compile_behavior_graph import situation_family


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    tmp.replace(path)


def private_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        os.chmod(tmp, 0o600)
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def aliases_from_exports(root: Path) -> set[str]:
    """Read known names locally; neither names nor identity map are exported."""
    manifest = json.loads((root / "collection.json").read_text())
    aliases = set()
    for entry in manifest["tasks"].values():
        if entry.get("status") != "completed":
            continue
        path = (root / entry["file"]).resolve()
        if not path.is_relative_to((root / "raw").resolve()):
            raise ValueError("source outside raw export directory")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError("source hash changed")
        for row in json.loads(payload)["messages"]:
            sender = row.get("sender") or {}
            for key in ("name", "nickname", "groupCard", "remark", "uid", "uin"):
                value = str(sender.get(key) or "")
                if len(value) >= 2 and value not in {"unknown", "未知用户"}:
                    aliases.add(value)
    return aliases


def clean_text(text: str, aliases: set[str] | None = None) -> str:
    for alias in sorted(aliases or (), key=lambda value: (-len(value), value)):
        text = text.replace(alias, "[称呼]")
    for pattern, replacement in (
        (r"@?user_[^\s]+", "[群友]"),
        (r"@?(?:user_|group_)[A-Za-z0-9_-]{6,}|u_[A-Za-z0-9_-]{10,}", "[群友]"),
        (r"https?://\S+|www\.\S+", "[链接]"),
        (r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱]"),
        (r"(?<!\d)\d{5,18}[Xx]?(?!\d)", "[数字]"),
        (r"(?:微信|wechat|vx|qq号)\s*[:：=]?\s*[A-Za-z0-9_-]{5,}", "[联系方式]"),
    ):
        text = re.sub(pattern, replacement, text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:500]


def packet(case, messages, aliases):
    # Supply preceding context plus the current incoming continuation. Do not
    # supply anything that arrived after the current message.
    tail = case["context_ids"][-8:]
    ids = list(dict.fromkeys([*tail, case["continuation_id"]]))
    local_ids = {mid: f"m{i + 1}" for i, mid in enumerate(ids)}
    speakers = {}
    context = []
    for mid in ids:
        row = messages[mid]
        speaker = speakers.setdefault(row["speaker_id"], f"S{len(speakers) + 1}")
        context.append({
            "id": local_ids[mid], "speaker": speaker,
            "text": clean_text(row["text"], aliases),
            "reply_to": [local_ids[ref] for ref in row.get("reply_to", []) if ref in local_ids],
            "unknown_media": "media_dependent" in row.get("flags", []),
            "is_current": mid == case["continuation_id"],
        })
    body = {"current_message_id": local_ids[case["continuation_id"]], "context": context}
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"case_id": case["id"], "split": "validation", "packet_sha256": fingerprint, **body}


def build(corpus: Path, output: Path, size: int, raw_root: Path) -> dict:
    if output.exists():
        raise ValueError("choose a new output; annotation packets are immutable")
    if not (corpus / "BUILD_COMPLETE.json").is_file():
        raise ValueError("corpus is incomplete")
    messages = {row["id"]: row for row in read_jsonl(corpus / "messages.jsonl")}
    cases = [row for row in read_jsonl(corpus / "training_candidates.jsonl") if row["split"] == "validation"]
    aliases = aliases_from_exports(raw_root)
    buckets = defaultdict(list)
    for case in cases:
        # This is only a sampling bucket, not a behavior label.
        target = messages[case["continuation_id"]]["text"]
        family, _ = situation_family(target, [])
        buckets[family].append(case)
    for rows in buckets.values():
        rows.sort(key=lambda row: hashlib.sha256(row["id"].encode()).hexdigest())
    selected, fingerprints = [], set()
    while len(selected) < size and any(buckets.values()):
        for family in sorted(buckets):
            if not buckets[family] or len(selected) >= size:
                continue
            case = buckets[family].pop(0)
            item = packet(case, messages, aliases)
            if item["packet_sha256"] in fingerprints:
                continue
            fingerprints.add(item["packet_sha256"])
            item["sample_id"] = f"v{len(selected) + 1:02d}"
            item["sampling_bucket"] = family
            selected.append(item)
    output.mkdir(parents=True, mode=0o700)
    private_jsonl(output / "packets.jsonl", selected)
    report = {
        "version": "annotation-packets-v2", "split": "validation",
        "cases": len(selected), "labels": "none; await actual model annotation",
        "sampling_buckets": dict(Counter(item["sampling_bucket"] for item in selected)),
        "task": "choose behavior for the marked current incoming message using preceding context",
        "future_messages_hidden": True, "media_bytes_missing": True,
        "privacy": "local sender aliases removed; free text still requires review",
    }
    private_json(output / "manifest.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--size", type=int, default=40)
    args = parser.parse_args()
    print(json.dumps(build(args.corpus, args.output, args.size, args.raw_root), ensure_ascii=False))


if __name__ == "__main__":
    main()
