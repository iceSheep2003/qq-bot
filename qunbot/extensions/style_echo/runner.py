"""The style echo object: collection loop, post-reply hook, context provider.

It is deliberately one small collaborator with four surfaces, because that is
what the feature host offers and nothing more:

``run``       a zero-argument background loop (``host.workers``) that re-scans
              the deployer's allow-listed groups on a timer.
``observe``   a post-reply observer (``host.observers``) that scans the scope of
              a turn that just happened, so a fresh sample does not wait a full
              poll interval.
``guidance``  a dynamic-context provider (``host.context``) that returns a
              restricted style note for the *current speaker* and ``None`` for
              anyone else.
``forget``    complete deletion for one subject.

The reader is an injected ``recent(scope, limit)`` callable. In production it is
``ConversationStore.recent`` over the same SQLite file the bot already uses, so
reading the group transcript needs no change to ``runtime/service.py`` and no
second copy of the conversation history. Tests inject a list-backed reader.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .config import StyleEchoConfig
from .guidance import analyze, render

log = logging.getLogger(__name__)

# Messages shorter than this are acknowledgements ("嗯", "在"), and messages
# longer than this are usually pasted text; neither describes how someone talks.
MIN_SAMPLE_CHARS = 2
MAX_SAMPLE_CHARS = 300


class StyleEcho:
    def __init__(
        self,
        config: StyleEchoConfig,
        store,
        recent: Callable[[str, int], list[dict]],
        groups: frozenset[str],
        *,
        now: Callable[[], float] = time.time,
    ):
        self.config = config
        self.store = store
        self.recent = recent
        self.groups = frozenset(groups)
        self.now = now

    # ---------------------------------------------------------------- consent

    def accepts(self, user_id: str) -> bool:
        """The only place collection consent is decided."""
        return self.config.accepts(user_id)

    def is_sample(self, text: str) -> bool:
        return MIN_SAMPLE_CHARS <= len(text) <= MAX_SAMPLE_CHARS

    # -------------------------------------------------------------- collection

    def collect(self, scope: str) -> int:
        """Scan one scope's recent messages and store allow-listed samples.

        A message is skipped — before anything is written — unless its author is
        on the allow-list. There is no branch that stores a non-allow-listed
        user "just in case"; the consent check is the first filter.
        """
        try:
            rows = self.recent(scope, self.config.scan_limit)
        except Exception:
            log.exception("Style echo could not read %s", scope)
            return 0
        stored = 0
        for row in rows or ():
            if row.get("role") != "user":
                continue
            user_id = str(row.get("user_id") or "")
            if not self.accepts(user_id):
                continue
            text = str(row.get("content") or "").strip()
            if not self.is_sample(text):
                continue
            if self.store.record(scope, user_id, text, int(self.now())):
                stored += 1
                self.store.prune(scope, user_id, self.config.max_samples)
        return stored

    def run_once(self) -> int:
        """One pass over every allow-listed group plus retention cleanup."""
        stored = 0
        for group_id in sorted(self.groups):
            stored += self.collect(f"group:{group_id}")
        cutoff = int(self.now()) - self.config.retention_days * 86_400
        self.store.forget_expired(cutoff)
        return stored

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.config.poll_seconds)
            try:
                self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Style echo collection pass failed")

    # ------------------------------------------------------- post-reply hook

    async def observe(self, event, bot_reply: str) -> None:
        """A turn just happened; scan its scope. Never touches the bot's reply.

        The reply is ignored on purpose: this feature learns how the *consenting
        user* writes, not how to answer, and it must not feed the bot's own text
        back into its style source.
        """
        if event.group_id:
            self.collect(event.scope)

    # --------------------------------------------------------- prompt surface

    def guidance(self, event) -> str | None:
        """A restricted style note for the current speaker, or ``None``.

        Scoped to the person who consented: the bot reflects a style back to the
        person whose style it is, rather than projecting one member's habits
        onto conversations with everyone else. A non-allow-listed speaker gets
        ``None``, so no learned data is ever surfaced for them.
        """
        if not self.accepts(str(event.user_id or "")):
            return None
        samples = self.store.samples(
            event.scope, str(event.user_id), self.config.max_samples
        )
        profile = analyze(samples, min_samples=self.config.min_samples)
        if profile is None:
            return None
        # ValueError from the guard is caught by the context registry, which
        # drops the contribution rather than the whole turn.
        return render(profile)

    # ------------------------------------------------------------- lifecycle

    def profile(self, scope: str, user_id: str) -> dict:
        """Read-only view for the deployer: how many samples, is a note ready."""
        samples = self.store.samples(scope, user_id, self.config.max_samples)
        profile = analyze(samples, min_samples=self.config.min_samples)
        return {
            "samples": len(samples),
            "ready": profile is not None,
            "guidance": render(profile) if profile is not None else "",
        }

    def forget(self, user_id: str) -> int:
        """Erase every sample for one person, across every group."""
        return self.store.forget_user(user_id)


def build_worker(service, gateway, config, mood):
    """Alternative wiring: run style echo as a BACKGROUND extension.

    A background extension factory is handed the live ``service``, so this path
    reads ``service.conversations.recent`` directly and opens no extra database
    connection. Use it *or* the feature path in ``__init__.register`` — never
    both, or two loops would scan the same groups.
    """
    from ...storage.style import StyleStore

    echo_config = StyleEchoConfig.from_env()
    # ConversationStore already exposes the connection and transaction boundary
    # SqliteRepository needs, so the style table rides on the bot's own database
    # and no second connection is opened.
    store = StyleStore(service.conversations)
    echo = StyleEcho(
        echo_config,
        store,
        service.conversations.recent,
        frozenset(config.group_allowlist),
    )
    return echo.run()
