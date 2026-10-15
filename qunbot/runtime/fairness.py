"""Small, in-process fairness controls for per-scope maintenance work.

Interactive turns already have one lock per scope. Background extraction and
compaction are different: a hot group can finish one job and immediately queue
another, occupying the shared model semaphore indefinitely.  This limiter
keeps a rolling per-scope budget.  It is deliberately advisory and in-memory;
the durable message ledger remains the source of truth after a restart.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class ScopeWorkBudget:
    """Bound maintenance admissions independently for every conversation."""

    def __init__(
        self,
        *,
        max_per_scope: int = 2,
        window_seconds: float = 60.0,
        clock=time.monotonic,
    ):
        if max_per_scope < 1 or window_seconds <= 0:
            raise ValueError("invalid scope work budget")
        self.max_per_scope = int(max_per_scope)
        self.window_seconds = float(window_seconds)
        self.clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def admit(self, scope: str) -> bool:
        """Reserve one maintenance slot, or refuse until the window advances."""
        now = self.clock()
        events = self._events[scope]
        cutoff = now - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.max_per_scope:
            return False
        events.append(now)
        return True

    def pending(self, scope: str) -> int:
        now = self.clock()
        events = self._events[scope]
        cutoff = now - self.window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        return len(events)
