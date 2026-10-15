"""Shared value returned by the optional cognitive decision port."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BehaviorDecision:
    """One turn's behavior contribution, ready to inject into the prompt.

    ``authority`` is the whole point: ``derived_read_only`` means the model may
    ignore it, ``binding`` means it should follow it. ``key`` is the prompt slot
    it occupies, so the two routes never overwrite each other's meaning.
    """

    mode: str
    source: str
    key: str
    authority: str
    block: dict[str, Any] = field(default_factory=dict)
