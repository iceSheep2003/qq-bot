"""Group-level speaking rhythm, derived from the existing conversation ledger.

This mode keeps only a bounded, in-memory profile. It never creates a second
copy of member messages or turns any individual's phrasing into a prompt.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import replace
from typing import Callable

from ...content import is_media_placeholder
from .config import StyleEchoConfig
from .guidance import analyze, render

log = logging.getLogger(__name__)

MAX_AGE_SECONDS = 7 * 86_400
MAX_PER_SPEAKER = 6
MIN_DISTINCT_SPEAKERS = 3


class GroupStyle:
    """Publish a safe, aggregate style note for a whitelisted QQ group."""

    def __init__(
        self,
        config: StyleEchoConfig,
        recent: Callable[[str, int], list[dict]],
        groups: frozenset[str],
        *,
        now: Callable[[], float] = time.time,
    ):
        self.config, self.recent = config, recent
        self.groups = frozenset(groups)
        self.now = now
        self._notes: dict[str, str] = {}

    def refresh(self, scope: str) -> None:
        if not scope.startswith("group:") or scope[6:] not in self.groups:
            return
        try:
            rows = self.recent(scope, max(self.config.scan_limit, self.config.max_samples * 4))
        except Exception:
            log.exception("Group style could not read %s", scope)
            return
        counts: Counter[str] = Counter()
        seen: set[str] = set()
        samples: list[str] = []
        cutoff = int(self.now()) - MAX_AGE_SECONDS
        for row in reversed(rows or []):
            if row.get("role") != "user":
                continue
            speaker = str(row.get("user_id") or "")
            content = str(row.get("content") or "").strip()
            stamp = int(row.get("created_at") or 0)
            if (
                not speaker or counts[speaker] >= MAX_PER_SPEAKER
                or (stamp and stamp < cutoff)
                or not 2 <= len(content) <= 120
                or content in seen
                or content == "+1"
                or content.startswith(("/", "!"))
                or "http://" in content or "https://" in content
                or is_media_placeholder(content)
            ):
                continue
            counts[speaker] += 1
            seen.add(content)
            samples.append(content)
            if len(samples) >= self.config.max_samples:
                break
        if len(counts) < MIN_DISTINCT_SPEAKERS:
            self._notes.pop(scope, None)
            return
        profile = analyze(samples, min_samples=self.config.min_samples)
        if profile is None:
            self._notes.pop(scope, None)
            return
        # A group can ask lots of questions or paste long explanations. Those
        # are not good habits for this bot to copy into every answer.
        profile = replace(
            profile,
            length="short" if profile.avg_chars < 18 else "medium",
            question=False,
        )
        self._notes[scope] = render(profile)

    def guidance(self, event) -> str | None:
        if not event.group_id or str(event.group_id) not in self.groups:
            return None
        return self._notes.get(event.scope)

    async def observe(self, event, _bot_reply: str) -> None:
        if event.group_id:
            self.refresh(event.scope)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_seconds)
            for group_id in sorted(self.groups):
                self.refresh(f"group:{group_id}")
