"""Short-term cache for context compiled from the long-term Markdown Wiki.

The Wiki remains the source of truth.  This cache only holds the small,
query-specific context selected for a turn.  Its key deliberately includes
scope, subject, query and Wiki version so a group, person or private chat can
never receive a fragment selected for another context.
"""

from __future__ import annotations

import copy
import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


WikiLoader = Callable[[str, str, str], Any]
CacheKey = tuple[str, str, str, str, str]


def _query_fingerprint(query: str) -> str:
    normalized = re.sub(r"\s+", " ", str(query or "").strip()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Entry:
    expires_at: float
    value: Any


class WikiContextCache:
    """Bounded TTL cache for query-specific Wiki context.

    ``loader`` is called only on a miss.  Failed loads are never cached, so a
    temporary Markdown or index read failure does not suppress later retries.
    Values are copied on both write and read: a prompt builder cannot mutate a
    cached context and accidentally change the next turn's context.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 300,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._entries: dict[CacheKey, _Entry] = {}
        self._hits = 0
        self._misses = 0
        self._loads = 0
        self._last_version = ""

    @property
    def size(self) -> int:
        self._purge_expired()
        return len(self._entries)

    def report(self) -> dict[str, int | float | str]:
        total = self._hits + self._misses
        return {
            "entries": self.size,
            "hits": self._hits,
            "misses": self._misses,
            "loads": self._loads,
            "hit_ratio": round(self._hits / total, 4) if total else 0.0,
            "last_version": self._last_version,
        }

    def get_or_load(
        self,
        scope: str,
        subject: str,
        query: str,
        wiki_version: str,
        loader: WikiLoader,
        *,
        read_mode: str = "selected",
    ) -> Any:
        """Return cached context or load the relevant Wiki fragments.

        A non-positive TTL disables caching while preserving the same loader
        contract, which is useful for tests and emergency deployments.
        """
        if self.ttl_seconds <= 0:
            self._misses += 1
            self._loads += 1
            self._last_version = str(wiki_version)
            return loader(str(scope), str(subject), str(query))

        key = (
            str(scope),
            str(subject),
            _query_fingerprint(query),
            str(wiki_version),
            str(read_mode),
        )
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None:
            if now < entry.expires_at:
                self._hits += 1
                return copy.deepcopy(entry.value)
            self._entries.pop(key, None)

        # Let loader exceptions propagate, but leave no entry behind.
        self._misses += 1
        value = loader(str(scope), str(subject), str(query))
        self._loads += 1
        self._last_version = str(wiki_version)
        self._entries[key] = _Entry(now + self.ttl_seconds, copy.deepcopy(value))
        self._trim(now)
        return copy.deepcopy(value)

    def invalidate_scope(self, scope: str) -> int:
        """Drop every cached fragment belonging to one conversation scope."""
        target = str(scope)
        removed = [key for key in self._entries if key[0] == target]
        for key in removed:
            self._entries.pop(key, None)
        return len(removed)

    def clear(self) -> None:
        self._entries.clear()

    def _purge_expired(self) -> None:
        now = self._clock()
        for key, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                self._entries.pop(key, None)

    def _trim(self, now: float) -> None:
        self._purge_expired()
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries, key=lambda key: self._entries[key].expires_at)
            self._entries.pop(oldest, None)
