"""Build evidence-backed behavior proposals from the private normalized corpus.

The script is intentionally resumable and private.  It sends only a bounded,
locally deidentified fragment to the configured model; raw QQ exports and
identity maps never leave the machine.  The output is a logical graph staging
artifact, not the online bot's memory database.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from qunbot.infrastructure.adapters.model import ModelClient
from qunbot.infrastructure.adapters.structured import InstructorOutput
from qunbot.infrastructure.extraction.behavior import BehaviorExtractor
from qunbot.agent.cognition.experience.models import BehaviorBatch


PIPELINE_VERSION = "behavior-graph-build-v1"
PSEUDONYM = re.compile(r"@?user_[A-Za-z0-9_-]{6,}")
GROUP_TOKEN = re.compile(r"group_[A-Za-z0-9_-]{6,}")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def atomic_json(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def append_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.chmod(path, 0o600)


def clean_text(value: str) -> str:
    value = PSEUDONYM.sub("[群友]", value)
    value = GROUP_TOKEN.sub("[群]", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:500]


def load_rows(root: Path) -> tuple[dict[str, dict], list[dict]]:
    messages = {
        row["id"]: row
        for row in (json.loads(line) for line in (root / "messages.jsonl").open(encoding="utf-8"))
    }
    cases = [
        json.loads(line)
        for line in (root / "training_candidates.jsonl").open(encoding="utf-8")
        if json.loads(line).get("split") == "train"
    ]
    return messages, cases


def case_for(case: dict, messages: dict[str, dict], max_messages: int) -> dict:
    ids = list(case["context_ids"][-max_messages:]) + [case["continuation_id"]]
    ids = list(dict.fromkeys(mid for mid in ids if mid in messages))
    speakers: dict[str, str] = {}
    rendered = []
    for index, mid in enumerate(ids, 1):
        row = messages[mid]
        speaker_id = row.get("speaker_id") or "unknown"
        speaker = speakers.setdefault(speaker_id, chr(ord("A") + len(speakers)))
        if len(speakers) > 8:
            speaker = "X"
        rendered.append(
            {
                "id": f"m{index}",
                "original_id": mid,
                "speaker": speaker,
                "is_reply_target": mid == case.get("reply_target_id"),
                "text": clean_text(row.get("text", "")),
            }
        )
    return {
        "id": case["id"],
        "message_ids": [row["id"] for row in rendered],
        "source_message_ids": [row["original_id"] for row in rendered],
        "messages": rendered,
    }


def map_proposal(proposal: dict, case: dict) -> dict:
    refs = {f"m{i + 1}": original for i, original in enumerate(case["source_message_ids"])}
    mapped = dict(proposal)
    mapped["source_message_ids"] = [refs[mid] for mid in proposal["source_message_ids"] if mid in refs]
    mapped["evidence"] = {
        "case_id": case["id"],
        "source_message_ids": mapped["source_message_ids"],
        "source_kind": "private_deidentified_corpus",
    }
    return mapped


async def extract_resilient(extractor: BehaviorExtractor, cases: list[dict]):
    """Retry malformed large batches by splitting them, preserving evidence."""
    try:
        return await extractor.extract(cases)
    except Exception:
        if len(cases) <= 1:
            raise
        middle = len(cases) // 2
        left, left_usage = await extract_resilient(extractor, cases[:middle])
        right, right_usage = await extract_resilient(extractor, cases[middle:])
        usage = {}
        for key in set(left_usage) | set(right_usage):
            values = [value for value in (left_usage.get(key), right_usage.get(key)) if isinstance(value, int)]
            if values:
                usage[key] = sum(values)
        return BehaviorBatch(proposals=left.proposals + right.proposals), usage


async def run(args: argparse.Namespace) -> None:
    corpus = args.corpus.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    manifest_path = corpus / "report.json"
    if not manifest_path.is_file() or not (corpus / "BUILD_COMPLETE.json").is_file():
        raise SystemExit("normalized corpus is incomplete or missing BUILD_COMPLETE.json")
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_hash = hashlib.sha256(
        (corpus / "BUILD_COMPLETE.json").read_bytes()
    ).hexdigest()
    messages, raw_cases = load_rows(corpus)
    cases = [case_for(case, messages, args.context_messages) for case in raw_cases]
    cases_by_id = {case["id"]: case for case in cases}

    checkpoint_path = output / "checkpoint.json"
    checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint_path.exists()
        else {
            "pipeline_version": PIPELINE_VERSION,
            "source_hash": source_hash,
            "next_case": 0,
            "processed": 0,
            "proposals": 0,
            "errors": 0,
        }
    )
    if checkpoint["source_hash"] != source_hash or checkpoint["pipeline_version"] != PIPELINE_VERSION:
        raise SystemExit("checkpoint belongs to another corpus or pipeline version")
    start = int(checkpoint["next_case"])
    if start < 0 or start > len(cases):
        raise SystemExit("invalid checkpoint cursor")

    cfg = read_env(args.env)
    required = {"BOT_MODEL_BASE_URL", "BOT_MODEL_API_KEY", "BOT_MODEL_NAME"}
    if required - cfg.keys():
        raise SystemExit("model env is incomplete")
    model = ModelClient(
        cfg["BOT_MODEL_BASE_URL"],
        cfg["BOT_MODEL_API_KEY"],
        cfg["BOT_MODEL_NAME"],
        timeout=args.timeout,
        max_completion_tokens=args.max_completion_tokens,
        # Extraction is a bounded schema transformation; long hidden
        # reasoning adds cost and was the reason the first 2600-token probe
        # ended without a JSON body.
        thinking_enabled=False,
    )
    extractor = BehaviorExtractor(InstructorOutput(model, max_attempts=2))
    limit = None if args.max_cases <= 0 else min(len(cases), start + args.max_cases)
    batch_size = max(1, min(16, args.batch_size))
    try:
        for offset in range(start, limit if limit is not None else len(cases), batch_size):
            batch = cases[offset : offset + batch_size]
            try:
                result, usage = await extract_resilient(extractor, batch)
                rows = [
                    map_proposal(proposal.model_dump(), cases_by_id[proposal.case_id])
                    for proposal in result.proposals
                ]
                append_jsonl(output / "behavior_proposals.jsonl", rows)
                checkpoint.update(
                    next_case=offset + len(batch),
                    processed=offset + len(batch),
                    proposals=int(checkpoint["proposals"]) + len(rows),
                    last_usage=usage,
                )
                atomic_json(checkpoint_path, checkpoint)
                print(
                    json.dumps(
                        {
                            "processed": checkpoint["processed"],
                            "total": len(cases),
                            "proposals": checkpoint["proposals"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - checkpointed batch boundary
                error = {
                    "batch_start": offset,
                    "batch_size": len(batch),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                    "time": int(time.time()),
                }
                append_jsonl(output / "errors.jsonl", [error])
                if len(batch) == 1:
                    append_jsonl(
                        output / "failed_cases.jsonl",
                        [{"case_id": batch[0]["id"], **error}],
                    )
                    checkpoint.update(
                        next_case=offset + 1,
                        processed=offset + 1,
                        errors=int(checkpoint["errors"]) + 1,
                    )
                    atomic_json(checkpoint_path, checkpoint)
                    continue
                checkpoint["errors"] = int(checkpoint["errors"]) + 1
                atomic_json(checkpoint_path, checkpoint)
                raise
    finally:
        await model.client.aclose()

    if checkpoint["next_case"] >= len(cases):
        atomic_json(
            output / "manifest.json",
            {
                "schema_version": 1,
                "pipeline_version": PIPELINE_VERSION,
                "source_hash": source_hash,
                "source_report": source_manifest,
                "cases": len(cases),
                "proposals": checkpoint["proposals"],
                "privacy": "private_deidentified_evidence_only",
                "status": "extracted_candidates",
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env", type=Path, default=Path("data/private/deployment/deepseek-20261014.env"))
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--context-messages", type=int, default=10)
    parser.add_argument("--max-cases", type=int, default=0, help="0 means all training cases")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--max-completion-tokens", type=int, default=2600)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
