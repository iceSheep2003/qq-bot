"""Memory replay: a read-only view over whatever memory already knows.

``qunbot/memory/`` is the single owner of memory data. This module owns none of
it — no table, no cache, no copy. It calls exactly one method on the existing
``MemoryCoordinator`` port, ``related(scope, query, limit)``, and formats the
strings that come back for the dynamic suffix. If the port is not supplied
(``memory=None`` and no ``host.memory_coordinator``), the provider is simply not
registered; the extension still starts.

The returned strings are a model's distillation of past group chatter, so they
are ``Trust.DERIVED``: useful, possibly stale, and never an instruction. They
land in the dynamic suffix alongside the Agent's own memory block and have no
path to the persona file.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_FLAT = re.compile(r"\s+")
#: Per-item ceiling; the registry's max_chars caps the whole contribution.
ITEM_MAX_CHARS = 80


def _flatten(item: object) -> str:
    return _FLAT.sub(" ", str(item)).strip()[:ITEM_MAX_CHARS]


class MemoryReplayProvider:
    """Contributes a short replay of relevant recollections for this scope."""

    def __init__(self, coordinator, limit: int = 3):
        self._coordinator = coordinator
        self._limit = max(1, int(limit))

    def __call__(self, event) -> list[str] | None:
        try:
            recalled = self._coordinator.related(event.scope, event.text, self._limit)
        except Exception as exc:
            # A broken memory read is this provider's problem, not the turn's.
            log.warning(
                "memory replay failed for scope=%s (%s); injecting nothing",
                event.scope,
                type(exc).__name__,
            )
            return None
        items = [_flatten(item) for item in (recalled or []) if str(item).strip()]
        return items or None
