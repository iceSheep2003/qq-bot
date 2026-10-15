"""Keep multi-speaker episodes intact and split them by event time."""

import re
from collections import Counter, defaultdict


def make_sessions(messages: list[dict], idle_seconds: int) -> list[dict]:
    if idle_seconds <= 0:
        raise ValueError("Idle threshold must be positive")
    by_group = defaultdict(list)
    for message in messages:
        by_group[message["group_id"]].append(message)
    result = []
    for group, rows in sorted(by_group.items()):
        rows.sort(
            key=lambda r: (
                r["timestamp_ms"],
                int(r["sequence"]) if r["sequence"].isdecimal() else 0,
                r["source"]["row"],
            )
        )
        episode = None
        for row in rows:
            if (
                episode is None
                or row["timestamp_ms"] - episode["end_ms"] > idle_seconds * 1000
            ):
                episode = {
                    "id": "session_" + row["id"][4:],
                    "group_id": group,
                    "start_ms": row["timestamp_ms"],
                    "end_ms": row["timestamp_ms"],
                    "message_ids": [],
                }
                result.append(episode)
            episode["end_ms"] = row["timestamp_ms"]
            episode["message_ids"].append(row["id"])
            row["session_id"] = episode["id"]
    return result


def temporal_split(sessions: list[dict], messages: list[dict]) -> dict:
    """Global time boundaries; spanning episodes are excluded, never sliced across sets."""
    times = sorted(r["timestamp_ms"] for r in messages)
    if not times:
        return {"counts": {}, "warning": "empty_corpus"}
    train_end, validation_end = (
        times[min(len(times) - 1, int(len(times) * 0.8))],
        times[min(len(times) - 1, int(len(times) * 0.9))],
    )
    counts = Counter()
    assignment = {}
    for episode in sessions:
        start, end = episode["start_ms"], episode["end_ms"]
        if end < train_end:
            split = "train"
        elif start >= train_end and end < validation_end:
            split = "validation"
        elif start >= validation_end:
            split = "test"
        else:
            split = "boundary_excluded"
        episode["split"] = split
        assignment[episode["id"]] = split
        counts[split] += len(episode["message_ids"])
    for message in messages:
        message["split"] = assignment[message["session_id"]]
    return {
        "train_end_ms_exclusive": train_end,
        "validation_end_ms_exclusive": validation_end,
        "message_counts": dict(counts),
        "all_sets_nonempty": all(counts[k] for k in ("train", "validation", "test")),
    }


SPAM = re.compile(
    r"(?:加.{0,5}(?:微信|vx)|代报名|代写|出售|售卖|包过|私聊.{0,6}(?:资料|课程)|低价.{0,5}(?:课|资料)|网盘.{0,5}链接)",
    re.I,
)


def rejection(message: dict) -> str | None:
    if message["speaker_kind"] in {"bot", "system"}:
        return "not_unclassified_user"
    if set(message["flags"]) & {
        "recalled",
        "media_dependent",
        "unresolved_reply",
        "text_fallback",
    }:
        return "unsupported_context"
    if set(message["flags"]) & {"url", "phone", "contact", "email", "id_number"}:
        return "sensitive_or_link"
    if not message["own_text"]:
        return "no_text"
    if len(message["own_text"]) > 500:
        return "long_message"
    if SPAM.search(message["own_text"]):
        return "possible_promotion"
    return None


def make_candidates(
    messages: list[dict],
    sessions: list[dict],
    context_messages: int = 40,
    context_chars: int = 6000,
) -> tuple[list[dict], list[dict], dict]:
    if context_messages < 2 or context_chars < 100:
        raise ValueError("Context budgets too small")
    lookup = {r["id"]: r for r in messages}
    candidates, replay = [], []
    reasons = Counter()
    for episode in sessions:
        if episode["split"] == "boundary_excluded":
            continue
        ids = episode["message_ids"]
        for position, mid in enumerate(ids):
            row = lookup[mid]
            reason = rejection(row)
            if reason:
                reasons[reason] += 1
                continue
            context = []
            length = 0
            for preceding in reversed(
                ids[max(0, position - context_messages) : position]
            ):
                prior = lookup[preceding]
                if length + len(prior["text"]) > context_chars:
                    break
                context.append(preceding)
                length += len(prior["text"])
            context.reverse()
            if len(context) < 2:
                reasons["short_context"] += 1
                continue
            example = {
                "id": "case_" + mid[4:],
                "group_id": row["group_id"],
                "session_id": episode["id"],
                "split": episode["split"],
                "context_ids": context,
                "continuation_id": mid,
                "label": "observed_continuation",
                "review_status": "unreviewed",
            }
            replay.append(example)
            refs = row["reply_to"]
            if len(refs) != 1:
                reasons["no_unique_explicit_quote"] += 1
                continue
            target = lookup.get(refs[0])
            if (
                refs[0] not in context
                or target is None
                or target["speaker_id"] == row["speaker_id"]
            ):
                reasons["quote_outside_context_or_self"] += 1
                continue
            if rejection(target) or any(
                rejection(lookup[x]) in {"possible_promotion", "sensitive_or_link"}
                for x in context
            ):
                reasons["unsuitable_quote_context"] += 1
                continue
            candidates.append(
                {
                    **example,
                    "label": "explicit_quote_continuation",
                    "reply_target_id": refs[0],
                    "human_authorship": "unverified",
                    "use_for_training": False,
                }
            )
    return candidates, replay, dict(reasons)
