"""Explicitly assembled feature contributions; no implicit file discovery.

``FeatureHost`` is the surface an extension registers against. What it may
touch is declared in ``qunbot/extensions/manifest.py`` and checked *during*
``register()`` by the loader: an extension that appends to ``observers``
without declaring the observer contribution fails at startup rather than
quietly widening what a package can do to the bot.

Shutdown goes through :meth:`FeatureHost.aclose`, which runs the closers in
reverse registration order and isolates failures — one extension's broken
cleanup must not leave another's database open.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from inspect import isawaitable
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

log = logging.getLogger(__name__)


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
    # Set by the loader before registration, so a feature that reads memories
    # at register time finds it. qunbot/memory stays the only owner of the
    # data; this is a read handle.
    memory_coordinator: object | None = None
    # Wiring callbacks, run once the conversation service exists. A feature
    # that needs a runtime collaborator appends one here; the loader does not
    # grow a parameter per feature, and app.py does not import feature modules
    # to wire them.
    binders: list[Callable[[object], None]] = field(default_factory=list)
    # Long-running background loops, started alongside the gateway and the
    # scheduler and cancelled on shutdown. Each is a zero-argument coroutine
    # function; a worker that raises is restarted by the supervisor, not by
    # the feature.
    workers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def media(self) -> MediaProcessor | None:
        if self.meme_source is None and self.speech_source is None:
            return None
        return ReplyMediaProcessor(self.meme_source or _NoMemes(), self.speech_source)

    async def aclose(self) -> None:
        """Shut every extension down, newest first.

        Closers are run in reverse registration order (a feature that depends
        on another's client is torn down before it), and one raising closer is
        logged and skipped rather than aborting the rest: shutdown must not be
        the step that leaks a database handle because an unrelated feature
        threw.
        """
        for close in reversed(self.closers):
            try:
                result = close()
                if isawaitable(result):
                    await result
            except Exception:
                log.exception("Feature closer %r failed during shutdown", close)


class _NoMemes:
    def pick(self, _tag: str) -> None:
        return None
