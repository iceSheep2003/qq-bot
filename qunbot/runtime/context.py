"""Named, bounded contributors to the dynamic (non-cacheable) prompt suffix."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..domain import MessageEvent


class ContextRegistry:
    def __init__(self):
        self._providers: dict[str, Callable[[MessageEvent], Any]] = {}

    def register(self, name: str, provider: Callable[[MessageEvent], Any]) -> None:
        if not name or name in self._providers:
            raise ValueError(f"invalid or duplicate context contributor: {name}")
        self._providers[name] = provider

    def collect(self, event: MessageEvent) -> dict[str, Any]:
        return {name: provider(event) for name, provider in self._providers.items()}
