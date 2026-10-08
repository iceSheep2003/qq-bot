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

from ...storage.slang import stage_for
from . import mining
from .config import SlangConfig
from .gloss import GlossEngine
from .review import ReviewFile

log = logging.getLogger(__name__)

# How many per-user and per-day keys are kept per term. The counts beyond this
# only feed the confidence score, which saturates long before these caps.
_MAX_TRACKED = 64


def _day(created_at: int) -> str:
    return time.strftime("%Y%m%d", time.gmtime(int(created_at)))


def _batches(rows: list[dict], size: int):
    size = max(1, int(size))
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


class SlangWorker:
    def __init__(
        self,
        store,
        config: SlangConfig,
        scopes: Iterable[str],
        reader: Callable[[str, int], list[dict]],
        *,
        review_file: ReviewFile | None = None,
        model=None,
        index=None,
    ):
        self.store = store
        self.config = config
        self.scopes = tuple(scopes)
        self.reader = reader
        self.review = review_file or ReviewFile(config.review_path)
        self.words = mining.load_wordlist(config.wordlist_path)
        # Absent model is a supported configuration, not a broken one: the
        # package still discovers and counts, it just cannot say what a term
        # means. Everything downstream treats "no meaning" as "not yet known".
        self.gloss = GlossEngine(model, config) if model is not None else None
        self._glossing: set[str] = set()
        # The worker publishes what the reply path may read: the provider runs
        # inside a turn, where a query is not allowed.
        self.index = index

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

        now = int(time.time())
        reviewed = self._apply_review(scope, now)
        # Decay before pruning. Eviction is by confidence, so a term that has
        # gone quiet must lose its confidence before `prune` can even consider
        # it; pruning first would keep the stale hits and drop the new ones.
        decayed = self.store.decay(
            scope, now=now, half_life=self.config.decay_half_life_seconds
        )
        pruned = self.store.prune(scope, self.config.max_candidates)
        if rows:
            self.store.advance_scan(scope, max(int(row.get("id") or 0) for row in rows))
        if self.index is not None:
            # Publish what this pass learned so the next turn can read it
            # without touching the database.
            self.index.refresh(scope)
        return {
            "scope": scope,
            "scanned": len(fresh),
            "new": len(merged),
            "kept": len(self.store.candidates(scope)),
            "reviewed": reviewed,
            "decayed": decayed,
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
            # Write at the storage floor, not the promotion floor. A term that
            # appears once per scan window can only cross the promotion floor
            # if its tally survives the window: discarding it here meant the
            # next pass started from 1 again, so it never did. Storage is cheap
            # and `prune` bounds it; invisibility was the real cost.
            if occurrences < self.config.store_occurrences:
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

    def _apply_review(self, scope: str, now: int) -> int:
        """Apply the deployer's file: status decisions and meanings both.

        A deployer's meaning is written with ``source="human"``, which is what
        stops a later inference pass from revising it.
        """
        review = self.review.current()
        if review.is_empty():
            return 0
        changed = 0
        for term, row in self.store.candidates(scope).items():
            action = review.action_for(scope, term)
            if action is not None:
                desired = review.status_for(scope, term)
                if desired != row["status"]:
                    self.store.set_status(scope, term, desired)
                    self.store.log_review(scope, term, action, "review file")
                    changed += 1

            if review.releases(scope, term):
                if self.store.clear_human_meaning(scope, term, now=now):
                    self.store.log_review(scope, term, "release", "review file")
                    changed += 1
                continue

            if not review.has_meaning(scope, term):
                continue
            meaning = review.meaning_for(scope, term) or ""
            if row["meaning_source"] == "human" and row["meaning"] == meaning:
                continue
            if self.store.set_meaning(
                scope, term, meaning, source="human", stage=0, now=now
            ):
                self.store.log_review(scope, term, "meaning", "review file")
                changed += 1
        return changed

    # -- meaning inference --------------------------------------------------

    async def gloss_pass(self, scope: str) -> int:
        """One bounded round of inference. Returns terms examined.

        Three gates stand between a scan and a model call, and all three are
        load-bearing rather than decorative — the default deployment wires its
        model client without a budget policy, so nothing else caps the spend:

        * ``gloss_enabled`` and an injected model;
        * the persisted interval, so a scope cannot be re-inferred every scan;
        * ``inference_stage`` per term, so evidence already judged is not
          judged again — and a restart resumes mid-staging instead of starting
          over.
        """
        if self.gloss is None or not self.config.gloss_enabled:
            return 0
        if self.config.gloss_max_per_scan <= 0 or scope in self._glossing:
            return 0
        now = int(time.time())
        if now - self.store.last_gloss(scope) < self.config.gloss_interval_seconds:
            return 0

        self._glossing.add(scope)
        try:
            rows = self.store.promotable(
                scope,
                thresholds=self.config.infer_thresholds,
                limit=self.config.gloss_max_per_scan,
                skip_human=not self.config.overwrite_human_meanings,
            )
            examined = 0
            for batch in _batches(rows, self.config.gloss_batch_size):
                examined += self._record(scope, batch, await self.gloss.infer_batch(batch), now)
            return examined
        except Exception:
            # A model that is down, misconfigured, or answering in prose must
            # not take the scan loop with it.
            log.exception("slang meaning inference failed for %s", scope)
            return 0
        finally:
            self._glossing.discard(scope)
            # Marked whether or not it worked: a broken model costs one attempt
            # per interval, not one per scan.
            self.store.mark_gloss(scope, now)

    def _record(self, scope: str, batch: list[dict], glossed, now: int) -> int:
        by_term = {str(row["term"]): row for row in batch}
        written = 0
        for item in glossed:
            row = by_term.get(item.term)
            if row is None:
                continue
            # An ordinary word is retired at the last stage: re-asking every
            # interval would spend on the same answer forever.
            stage = (
                stage_for(int(row["occurrences"]), self.config.infer_thresholds)
                if item.is_jargon
                else len(self.config.infer_thresholds)
            )
            if self.store.set_meaning(
                scope,
                item.term,
                item.meaning,
                source="llm",
                stage=stage,
                now=now,
                context_meaning=item.context_meaning,
                standalone_meaning=item.standalone_meaning,
            ):
                written += 1
        return written

    # -- loop ---------------------------------------------------------------

    async def run(self, *, sleep=asyncio.sleep) -> None:
        while True:
            for scope in self.scopes:
                try:
                    self.scan(scope)
                    await self.gloss_pass(scope)
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
