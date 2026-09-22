"""Explicitly assembled feature contributions; no implicit file discovery."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Callable

from ..runtime.context import ContextRegistry
from ..ports import (
    MediaProcessor,
    MoodObserver,
    ReplyDecisionPolicy,
    ReplyObserver,
)
from ..runtime.tools import ToolRegistry
from .media import ReplyMediaProcessor


@dataclass
class FeatureHost:
    context: ContextRegistry = field(default_factory=ContextRegistry)
    observers: list[ReplyObserver] = field(default_factory=list)
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    closers: list[Callable] = field(default_factory=list)
    meme_source: object | None = None
    speech_source: object | None = None
    proactive_gate: MoodObserver | None = None
    # Replaces the built-in "@ me only" rule. None keeps the default.
    reply_policy: ReplyDecisionPolicy | None = None
    # Long-running background loops, started alongside the gateway and the
    # scheduler and cancelled on shutdown. Each is a zero-argument coroutine
    # function; a worker that raises is restarted by the supervisor, not by
    # the feature.
    workers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def media(self) -> MediaProcessor | None:
        if self.meme_source is None and self.speech_source is None:
            return None
        return ReplyMediaProcessor(self.meme_source or _NoMemes(), self.speech_source)


class _NoMemes:
    def pick(self, _tag: str) -> None:
        return None
