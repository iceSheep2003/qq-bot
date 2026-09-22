"""Explicitly assembled feature contributions; no implicit file discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..runtime.context import ContextRegistry
from ..ports import MediaProcessor, MoodObserver, ReplyObserver
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

    def media(self) -> MediaProcessor | None:
        if self.meme_source is None and self.speech_source is None:
            return None
        return ReplyMediaProcessor(self.meme_source or _NoMemes(), self.speech_source)


class _NoMemes:
    def pick(self, _tag: str) -> None:
        return None
