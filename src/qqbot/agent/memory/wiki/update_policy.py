"""Publication policy for keeping Wiki updates cheap and cache-friendly."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WikiUpdatePolicy:
    """Batch accepted facts before changing a published Wiki version.

    Candidates and evidence may be written immediately.  The published
    version changes only when a batch is large enough, old enough, or forced,
    so prompt and Wiki-context caches are not invalidated on every message.
    """

    min_interval_seconds: int = 600
    max_pending_entries: int = 8

    def should_publish(
        self,
        *,
        now: int,
        last_published_at: int | None,
        pending_entries: int,
        force: bool = False,
    ) -> bool:
        if force and pending_entries > 0:
            return True
        if pending_entries >= self.max_pending_entries:
            return True
        if pending_entries <= 0 or last_published_at is None:
            return False
        return now - last_published_at >= self.min_interval_seconds


def published_version(value: Any) -> str:
    """Return a deterministic version for a published, canonical snapshot."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

