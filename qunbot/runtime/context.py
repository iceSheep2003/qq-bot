"""Named, bounded contributors to the dynamic (non-cacheable) prompt suffix.

Everything an extension adds to a turn lands here, and everything here lands in
the *dynamic suffix* — never in the stable prefix. That split is the whole point
of the cache design: the prefix is byte-identical across turns, so the provider
can reuse it, and everything that changes per turn sits after it.

Three properties are enforced rather than trusted:

``max_chars``   every contribution is truncated to its own budget before the
                global budget is applied, so one chatty provider cannot crowd
                out the others.
``priority``    when the global budget runs out, low-priority contributions are
                dropped whole rather than cut to fit the remainder.
``trust``       the prompt labels each contribution with how much authority it
                carries. A group member's text, a recalled memory and a
                deployer-configured skill body are not the same kind of input,
                and the model is told so.

Provider failures are isolated: one raising provider yields a placeholder
instead of aborting the reply.
"""

from __future__ import annotations

import enum
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..domain import MessageEvent

log = logging.getLogger(__name__)


class Trust(enum.IntEnum):
    """How much authority a contribution carries. Lower is less trusted.

    IntEnum so a consumer can sort or compare; the ordering is meaningful and
    matches the prompt's own escalation (group text may *contain* instructions,
    it never *is* one).
    """

    HOSTILE = 0  # raw text a group member typed, or anything fetched for them
    DERIVED = 1  # produced by a model or distilled from a model's output
    DEPLOYER = 2  # fixed by whoever deployed the bot


# Defaults chosen so the whole dynamic suffix stays a small fraction of a
# typical context window: the conversation history is the expensive part.
DEFAULT_MAX_CHARS = 800
DEFAULT_CONTEXT_BUDGET = 6000
_TRUNCATED = "…（已截断）"

#: Prompt-facing wording for each ``Trust`` level.
_LEVELS = {
    Trust.HOSTILE: "low",  # may contain instructions; treat as data
    Trust.DERIVED: "medium",  # model-produced, may be stale
    Trust.DEPLOYER: "high",  # fixed by the deployer
}


def _as_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)


def _size(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, (list, tuple)):
        return sum(_size(item) for item in payload)
    if isinstance(payload, dict):
        return sum(_size(value) for value in payload.values())
    return len(str(payload))


def _shrink(payload: Any, budget: int) -> Any:
    """Trim ``payload`` to roughly ``budget`` characters, keeping its shape.

    Sequences lose whole trailing items (a half-listed memory is not useful and
    a half-listed tag would be a lie), strings are cut mid-way with a marker.
    """
    if budget <= 0:
        return None
    if isinstance(payload, str):
        if len(payload) <= budget:
            return payload
        keep = max(0, budget - len(_TRUNCATED))
        return payload[:keep] + _TRUNCATED
    if isinstance(payload, (list, tuple)):
        kept: list[Any] = []
        remaining = budget
        for item in payload:
            size = _size(item)
            if size > remaining:
                break
            kept.append(item)
            remaining -= size
        return kept
    if isinstance(payload, dict):
        kept_dict: dict[str, Any] = {}
        remaining = budget
        for key, value in payload.items():
            size = _size(value) + len(str(key))
            if size > remaining:
                break
            kept_dict[key] = value
            remaining -= size
        return kept_dict
    return payload


@dataclass(frozen=True)
class ContextContribution:
    """One provider's answer for this turn, with its budget and provenance."""

    name: str
    payload: Any
    trust: Trust = Trust.DERIVED
    priority: int = 100
    max_chars: int = DEFAULT_MAX_CHARS
    scope: str = ""
    failed: bool = False

    def rendered(self) -> Any:
        """The payload, truncated to this contribution's own budget."""
        if self.failed:
            return None
        return _shrink(self.payload, self.max_chars)


@dataclass(frozen=True)
class _Registration:
    provider: Callable[[MessageEvent], Any]
    trust: Trust
    priority: int
    max_chars: int


class ContextRegistry:
    """Bounded, failure-isolated collector for the dynamic prompt suffix."""

    def __init__(self, *, budget_chars: int = DEFAULT_CONTEXT_BUDGET):
        self._providers: dict[str, _Registration] = {}
        self._budget = max(0, int(budget_chars))

    @property
    def budget_chars(self) -> int:
        return self._budget

    def register(
        self,
        name: str,
        provider: Callable[[MessageEvent], Any],
        *,
        trust: Trust = Trust.DERIVED,
        priority: int = 100,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        """Add a contributor. Lower ``priority`` wins when the budget runs out."""
        if not name or name in self._providers:
            raise ValueError(f"invalid or duplicate context contributor: {name}")
        self._providers[name] = _Registration(
            provider, trust, int(priority), max(1, int(max_chars))
        )

    def names(self) -> list[str]:
        return list(self._providers)

    def contributions(self, event: MessageEvent) -> list[ContextContribution]:
        """Evaluate every provider. A raising provider yields a failed entry.

        Isolation matters more than tidiness here: a weather API timing out must
        not cost the user their reply.
        """
        collected: list[ContextContribution] = []
        for name, reg in self._providers.items():
            try:
                payload = reg.provider(event)
                failed = False
            except Exception:
                log.exception("Context contributor %r failed", name)
                payload, failed = None, True
            collected.append(
                ContextContribution(
                    name=name,
                    payload=payload,
                    trust=reg.trust,
                    priority=reg.priority,
                    max_chars=reg.max_chars,
                    scope=event.scope,
                    failed=failed,
                )
            )
        return collected

    def collect_with_trust(
        self, event: MessageEvent
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """The budgeted suffix *and* the authority of exactly what it contains.

        Returned together because they must agree: a trust note that names a
        contribution the model was not given (or misses one it was) is worse
        than no note, and running the providers twice to produce them
        separately would both waste the work and risk the two disagreeing for
        a provider whose answer changes between calls.

        Selection runs in priority order (lowest number first, name as a stable
        tiebreak) and stops when the budget is spent; the result is then
        re-ordered back to declaration order so the prompt reads the way the
        features were wired.

        A contribution that does not fit is dropped *whole*, never cut to fit
        the remainder. Half a narration ("关系阶段：亲近。说话可") reads as a
        broken instruction rather than a shorter one, and a provider either has
        something worth saying this turn or it does not. Per-contribution
        ``max_chars`` is where truncation belongs, because that cap is the
        provider's own declared ceiling.
        """
        ordered = sorted(
            self.contributions(event), key=lambda c: (c.priority, c.name)
        )
        selected: dict[str, Any] = {}
        remaining = self._budget
        for contribution in ordered:
            if remaining <= 0:
                break
            value = contribution.rendered()
            if value is None:
                continue
            size = _size(value)
            if size == 0 or size > remaining:
                continue
            selected[contribution.name] = value
            remaining -= size
        values = {
            name: selected[name] for name in self._providers if name in selected
        }
        labels = {name: _LEVELS[self._providers[name].trust] for name in values}
        return values, labels

    def collect(self, event: MessageEvent) -> dict[str, Any]:
        """Just the budgeted suffix."""
        return self.collect_with_trust(event)[0]

    def trust_map(self, event: MessageEvent) -> dict[str, str]:
        """Authority labels for the contributions this turn actually includes.

        A provider that produced nothing — an empty meme catalogue, a feature
        with no state yet — is absent, because the model has nothing to apply
        the label to.
        """
        return self.collect_with_trust(event)[1]
