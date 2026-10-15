"""Reproducible offline build from verified completed exports."""

import hashlib
import json
import os
import secrets
from collections import Counter
from pathlib import Path

from .normalize import normalize
from .privacy import IdentityMap
from .qce import write_json
from .sessions import make_candidates, make_sessions, temporal_split


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        for row in rows:
            stream.write(
                json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            )


def build(
    root: Path,
    output: Path,
    idle_seconds: int = 1200,
    bot_uins: list[str] | None = None,
    context_messages: int = 40,
    completed_only: bool = False,
) -> dict:
    os.umask(0o077)
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise ValueError(
            "Output already exists; choose a new directory to preserve previous builds"
        )
    manifest = json.loads((root / "collection.json").read_text(encoding="utf-8"))
    sources = []
    skipped_groups = []
    groups = list(manifest["groups"])
    if completed_only:
        groups = [
            group
            for group in groups
            if manifest["tasks"].get(group, {}).get("status") == "completed"
        ]
        skipped_groups = [group for group in manifest["groups"] if group not in groups]
    for group in groups:
        entry = manifest["tasks"].get(group, {})
        if entry.get("status") != "completed" or "sha256" not in entry:
            raise ValueError(
                "Every allowlisted group must have a verified completed export"
            )
        path = (root / entry["file"]).resolve()
        if not path.is_relative_to((root / "raw").resolve()):
            raise ValueError("Source path escapes raw directory")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise ValueError("Raw export hash changed")
        data = json.loads(payload)
        if (
            data.get("chatInfo", {}).get("type") != "group"
            or str(data["chatInfo"].get("peerUid")) != group
        ):
            raise ValueError(
                "Export group identity does not match the authorized collection"
            )
        rows = data.get("messages")
        if not isinstance(rows, list) or len(rows) != entry["message_count"]:
            raise ValueError("Message count differs from completed export")
        sources.append((group, entry["sha256"], rows))
    salt_path = root / "pseudonym.key"
    if not salt_path.exists():
        with salt_path.open("xb") as stream:
            os.chmod(salt_path, 0o600)
            stream.write(secrets.token_bytes(32))
    salt_path.chmod(0o600)
    identities = IdentityMap(salt_path.read_bytes())
    # A QQ export can reuse a transport alias with different canonical sender
    # metadata across independently exported groups.  The behavior corpus
    # does not need cross-group identity joins, so completed-only builds keep
    # identity maps scoped to each source instead of weakening the conflict
    # check or creating a false cross-group person link.
    identity_maps = {}
    if completed_only:
        for group, _, rows in sources:
            scoped = IdentityMap(identities.salt, strict_conflicts=False)
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("sender"), dict):
                    scoped.register(row["sender"])
            scoped.seal()
            identity_maps[group] = scoped
        bots_by_group = {
            group: {scoped.person(uin) for uin in (bot_uins or [])}
            for group, scoped in identity_maps.items()
        }
    else:
        for _, _, rows in sources:
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("sender"), dict):
                    identities.register(row["sender"])
        identities.seal()
        bots = {identities.person(uin) for uin in (bot_uins or [])}
        bots_by_group = {group: bots for group, _, _ in sources}
    messages, quarantine, duplicates, group_stats = [], [], [], []
    seen = {}
    for group, digest, rows in sources:
        accepted, flags, reasons = [], Counter(), Counter()
        scoped_identities = identity_maps.get(group, identities)
        for index, row in enumerate(rows):
            try:
                if not isinstance(row, dict):
                    raise ValueError("invalid_row")
                message = normalize(
                    row,
                    group,
                    digest,
                    index,
                    scoped_identities,
                    bots_by_group[group],
                )
            except ValueError as exc:
                reasons[str(exc)] += 1
                quarantine.append(
                    {"source_sha256": digest, "row": index, "reason": str(exc)}
                )
                continue
            if message["id"] in seen:
                original = seen[message["id"]]
                same = all(
                    original[k] == message[k]
                    for k in ("timestamp_ms", "speaker_id", "text", "reply_to")
                )
                if not same:
                    raise ValueError(
                        "Conflicting contents for the same message identity; inspect raw source"
                    )
                duplicates.append({"id": message["id"], "source": message["source"]})
                continue
            seen[message["id"]] = message
            accepted.append(message)
            messages.append(message)
            flags.update(message["flags"])
        group_stats.append(
            {
                "group_id": identities.token("group", group),
                "raw_messages": len(rows),
                "accepted_messages": len(accepted),
                "quarantine": dict(reasons),
                "flags": dict(flags),
                "speakers": len({m["speaker_id"] for m in accepted}),
                "first_time_utc": min((m["time_utc"] for m in accepted), default=None),
                "last_time_utc": max((m["time_utc"] for m in accepted), default=None),
                "source_sha256": digest,
            }
        )
    sessions = make_sessions(messages, idle_seconds)
    split = temporal_split(sessions, messages)
    candidates, replay, reasons = make_candidates(messages, sessions, context_messages)
    known_bots = sum(len(value) for value in bots_by_group.values())
    # Annotation IDs only; no names/UID maps are written to derived data.
    report = {
        "schema_version": 1,
        "scope": manifest["scope"],
        "groups": group_stats,
        "messages": len(messages),
        "sessions": len(sessions),
        "duplicate_records": len(duplicates),
        "quarantined_records": len(quarantine),
        "replay_cases": len(replay),
        "training_candidates": len(candidates),
        "candidate_rejections": reasons,
        "split": split,
        "parameters": {
            "idle_seconds": idle_seconds,
            "context_messages": context_messages,
            "context_chars": 6000,
            "known_bots": known_bots,
        },
        "privacy": "pseudonymized_local_only_requires_manual_privacy_review",
        "limitations": [
            "locally_readable_history_not_guaranteed_complete",
            "speaker_authorship_unverified",
            "no_media_contents",
            "idle_episode_is_not_a_topic",
            "candidates_are_not_gold_labels",
        ],
    }
    if skipped_groups:
        report["skipped_groups"] = skipped_groups
        report["limitations"].append(
            "incomplete_exports_excluded_by_explicit_completed_only_mode"
        )
    if completed_only and any(scoped.conflicts for scoped in identity_maps.values()):
        report["identity_conflicts"] = {
            group: scoped.conflicts
            for group, scoped in identity_maps.items()
            if scoped.conflicts
        }
        report["limitations"].append(
            "source_identity_alias_conflicts_scoped_and_not_used_for_behavior_links"
        )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.mkdir(mode=0o700)
    try:
        write_jsonl(
            output / "messages.jsonl",
            sorted(
                messages,
                key=lambda m: (m["timestamp_ms"], m["group_id"], m["sequence"]),
            ),
        )
        write_jsonl(output / "sessions.jsonl", sessions)
        write_jsonl(output / "replay_cases.jsonl", replay)
        write_jsonl(output / "training_candidates.jsonl", candidates)
        write_jsonl(output / "quarantine.jsonl", quarantine)
        write_jsonl(output / "duplicates.jsonl", duplicates)
        write_json(output / "report.json", report)
        write_json(
            output / "BUILD_COMPLETE.json",
            {
                "schema_version": 1,
                "source_hashes": [d for _, d, _ in sources],
                "parameters": report["parameters"],
                "salt_fingerprint": hashlib.sha256(identities.salt).hexdigest(),
            },
        )
    except BaseException:
        # Keep incomplete output for diagnosis. Its missing completion marker forbids downstream use.
        raise
    return report
