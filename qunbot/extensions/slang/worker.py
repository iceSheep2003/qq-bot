"""The background scan: read new group messages, count, review, prune.

One pass reads a bounded recent window per scope, counts candidate chunks in
the messages newer than the last watermark, merges the counts into the store
and applies the reviewer's file. No model call, no message sent, no group
member visible to it.

The watermark is what keeps a message from being counted twice: a restart, a
re-scan or a duplicated frame all resume from the same id.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Iterable

from . import mining
from .config import SlangConfig
from .review import ReviewFile

log = logging.getLogger(__name__)

# How many per-user and per-day keys are kept per term. The counts beyond this
# only feed the confidence score, which saturates long before these caps.
_MAX_TRACKED = 64


def _day(created_at: int) -> str:
    return time.strftime("%Y%m%d", time.gmtime(int(created_at)))


class SlangWorker:
    def __init__(
        self,
        store,
        config: SlangConfig,
        scopes: Iterable[str],
        reader: Callable[[str, int], list[dict]],
        *,
        review_file: ReviewFile | None = None,
    ):
        self.store = store
        self.config = config
        self.scopes = tuple(scopes)
        self.reader = reader
        self.review = review_file or ReviewFile(config.review_path)
        self.words = mining.load_wordlist(config.wordlist_path)

    # -- one scope ----------------------------------------------------------

    def scan(self, scope: str) -> dict:
        """One pass over one scope. Returns counters for logging and tests."""
        window = max(1, int(self.config.window_messages))
        try:
            rows = self.reader(scope, window) or []
        except Exception:
            log.exception("slang scan could not read messages for %s", scope)
            return {"scope": scope, "scanned": 0, "new": 0, "kept": 0}

        watermark = self.store.last_scan(scope)
        fresh = [
            row
            for row in rows
            if int(row.get("id") or 0) > watermark and row.get("role") == "user"
        ]
        merged = self._merge(scope, fresh)
        self._write(scope, merged)

        reviewed = self._apply_review(scope)
        pruned = self.store.prune(scope, self.config.max_candidates)
        if rows:
            self.store.advance_scan(scope, max(int(row.get("id") or 0) for row in rows))
        return {
            "scope": scope,
            "scanned": len(fresh),
            "new": len(merged),
            "kept": len(self.store.candidates(scope)),
            "reviewed": reviewed,
            "pruned": pruned,
        }

    def _merge(self, scope: str, rows: list[dict]) -> dict[str, dict]:
        """Fold new messages into the stored evidence without double-counting."""
        merged: dict[str, dict] = {}
        for row in rows:
            text = mining.normalize(
                row.get("content", ""), max_chars=self.config.max_message_chars
            )
            if not text:
                continue
            user_id = str(row.get("user_id", ""))
            day = _day(row.get("created_at") or 0)
            snippet = str(row.get("content", ""))[: self.config.max_sample_chars]
            for term in mining.candidates_from(
                text,
                min_chars=self.config.min_term_chars,
                max_chars=self.config.max_term_chars,
                max_ngram=self.config.max_ngram,
            ):
                if mining.is_noise(
                    term,
                    self.words,
                    min_chars=self.config.min_term_chars,
                    max_chars=self.config.max_term_chars,
                ):
                    continue
                entry = merged.setdefault(
                    term, {"occurrences": 0, "users": [], "days": [], "samples": []}
                )
                entry["occurrences"] += 1
                if user_id and user_id not in entry["users"]:
                    entry["users"].append(user_id)
                if day not in entry["days"]:
                    entry["days"].append(day)
                if len(entry["samples"]) < self.config.max_samples:
                    entry["samples"].append(
                        {"user_id": user_id, "at": int(row.get("created_at") or 0), "text": snippet}
                    )

        if not merged:
            return {}
        stored = self.store.candidates(scope)
        now = int(time.time())
        result: dict[str, dict] = {}
        for term, entry in merged.items():
            previous = stored.get(term)
            users = list(entry["users"])
            days = list(entry["days"])
            samples = list(entry["samples"])
            occurrences = entry["occurrences"]
            if previous:
                occurrences += int(previous["occurrences"])
                for user in _json_list(previous["seen_users"]):
                    if user not in users:
                        users.append(user)
                for day in _json_list(previous["seen_days"]):
                    if day not in days:
                        days.append(day)
                for sample in _json_list(previous["samples"]):
                    if len(samples) >= self.config.max_samples:
                        break
                    samples.append(sample)
            users, days = users[:_MAX_TRACKED], days[:_MAX_TRACKED]
            samples = samples[: self.config.max_samples]
            confidence = mining.confidence(occurrences, len(users), len(days))
            # Below the evidence floor a term is not recorded at all: a single
            # sighting is a typo far more often than it is a group's word, and
            # storing every 2-gram once would bury the real candidates.
            if occurrences < self.config.min_occurrences:
                continue
            result[term] = {
                "occurrences": occurrences,
                "users": users,
                "days": days,
                "samples": samples,
                "confidence": confidence,
                "now": now,
            }
        return result

    def _write(self, scope: str, merged: dict[str, dict]) -> None:
        for term, entry in merged.items():
            self.store.upsert(
                scope,
                term,
                occurrences=entry["occurrences"],
                seen_users=entry["users"],
                seen_days=entry["days"],
                samples=entry["samples"],
                confidence=entry["confidence"],
                now=entry["now"],
            )

    def _apply_review(self, scope: str) -> int:
        review = self.review.current()
        if review.is_empty():
            return 0
        changed = 0
        for term, row in self.store.candidates(scope).items():
            action = review.action_for(scope, term)
            if action is None:
                continue
            desired = review.status_for(scope, term)
            if desired == row["status"]:
                continue
            self.store.set_status(scope, term, desired)
            self.store.log_review(scope, term, action, "review file")
            changed += 1
        return changed

    # -- loop ---------------------------------------------------------------

    async def run(self, *, sleep=asyncio.sleep) -> None:
        while True:
            for scope in self.scopes:
                try:
                    self.scan(scope)
                except Exception:
                    # One bad scope must not stop the others, and nothing here
                    # is allowed to take the gateway down.
                    log.exception("slang scan failed for %s", scope)
            await sleep(max(30, int(self.config.scan_interval_seconds)))


def _json_list(raw) -> list:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []
