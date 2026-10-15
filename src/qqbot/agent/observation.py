"""Bounded post-reply observation lifecycle.

The conversation use case owns when an observation should be emitted. This
module owns how advisory consumers are admitted, executed, recovered and
stopped. It has no platform or storage implementation dependencies.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any

from qqbot.actions.replies.models import ReplyDraft
from qqbot.contracts.models import MessageEvent

log = logging.getLogger("qqbot.agent.service")
DEFAULT_BACKLOG = 256
DEFAULT_WORKERS = 1
DEFAULT_DEDUPE = 4096
_REPLAYABLE_HANDLERS = {
    "qqbot.agent.memory.relationships.evaluator.AffectionEvaluator",
    "qqbot.agent.identity.emotion.system.EmotionSystem",
}


@dataclass
class ObservationStats:
    enqueued: int = 0
    dropped: int = 0
    deduplicated: int = 0
    completed: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _DraftObserver:
    observer: object
    draft: object

    async def observe(self, event: MessageEvent, reply: str) -> None:
        await self.observer.observe_draft(event, reply, self.draft)


class ObservationDispatcher:
    """Single owner for bounded, at-most-once post-reply work."""

    def __init__(
        self,
        *,
        affection: object | None = None,
        observers: tuple[object, ...] = (),
        affection_enabled: bool = False,
        backlog: int = DEFAULT_BACKLOG,
        workers: int = DEFAULT_WORKERS,
        dedupe: int = DEFAULT_DEDUPE,
        store: Any | None = None,
    ) -> None:
        if backlog < 1 or workers < 1 or dedupe < 1:
            raise ValueError("observation bounds must be positive")
        self.affection = affection
        self.observers = tuple(observers)
        self.affection_enabled = affection_enabled
        self.backlog, self.worker_count, self.dedupe_limit = backlog, workers, dedupe
        self.store = store
        self.stats = ObservationStats()
        self._queue: asyncio.Queue | None = None
        self._workers: list[asyncio.Task] = []
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._handlers: dict[str, object] = {}
        self._durable_tasks: set[asyncio.Task] = set()
        self._closed = False
        for observer in (affection, *self.observers):
            if observer is not None:
                self._register(observer)

    def _register(self, observer: object) -> str:
        identity = type(observer).__module__ + "." + type(observer).__qualname__
        self._handlers[identity] = observer
        return identity

    @staticmethod
    def _with_draft(observer: object, draft: object | None) -> object:
        if (draft is not None and getattr(draft, "state_observations", None) is not None
                and callable(getattr(observer, "observe_draft", None))):
            return _DraftObserver(observer, draft)
        return observer

    def dispatch(self, event: MessageEvent, reply: str, draft: object | None = None) -> None:
        if self.affection is not None and self.affection_enabled and event.group_id:
            self.submit(f"affection:{event.event_id}", self._with_draft(self.affection, draft), event, reply)
        for index, observer in enumerate(self.observers):
            self.submit(f"observer:{index}:{event.event_id}", self._with_draft(observer, draft), event, reply)

    def submit(self, key: str, observer: object, event: MessageEvent, reply: str) -> bool:
        if self._closed:
            return False
        base = observer.observer if isinstance(observer, _DraftObserver) else observer
        identity = self._register(base)
        draft = (asdict(observer.draft) if isinstance(observer, _DraftObserver)
                 and isinstance(observer.draft, ReplyDraft) else None)
        if self.store is not None:
            replayable = identity in _REPLAYABLE_HANDLERS
            status = self.store.enqueue(key, identity, asdict(event), reply, draft,
                                        replayable=replayable, capacity=self.backlog)
            if status == "queued":
                self.stats.enqueued += 1
                return True
            if status == "duplicate":
                self.stats.deduplicated += 1
            else:
                self.stats.dropped += 1
            return False
        if key in self._seen:
            self.stats.deduplicated += 1
            return False
        self._seen[key] = None
        while len(self._seen) > self.dedupe_limit:
            self._seen.popitem(last=False)
        queue = self._ensure_queue()
        try:
            queue.put_nowait((key, observer, event, reply))
        except asyncio.QueueFull:
            self.stats.dropped += 1
            log.warning("Observation backlog full; dropped %s", key)
            return False
        self.stats.enqueued += 1
        return True

    def _ensure_queue(self) -> asyncio.Queue:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.backlog)
        if not any(not task.done() for task in self._workers):
            self._workers = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]
        return self._queue

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            key, observer, event, reply = await self._queue.get()
            try:
                await observer.observe(event, reply)
                self.stats.completed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats.failed += 1
                log.exception("Post-reply observation failed for %s", key)
            finally:
                self._queue.task_done()

    @property
    def pending(self) -> int:
        if self.store is not None:
            return self.store.pending()
        return self._queue.qsize() if self._queue is not None else 0

    async def run_durable(self) -> None:
        if self.store is None or self._closed:
            return
        current = asyncio.current_task()
        if current is not None:
            self._durable_tasks.add(current)
        try:
            self.store.recover()
            while not self._closed:
                row = self.store.lease(tuple(self._handlers))
                if row is None:
                    await asyncio.sleep(0.05)
                    continue
                observer = self._handlers[row["handler_id"]]
                try:
                    incoming = MessageEvent(**json.loads(row["event_json"]))
                    if row["draft_json"] and callable(getattr(observer, "observe_draft", None)):
                        draft = ReplyDraft(**json.loads(row["draft_json"]))
                        await observer.observe_draft(incoming, row["reply"], draft)
                    else:
                        await observer.observe(incoming, row["reply"])
                    self.store.complete(row["id"])
                    self.stats.completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.store.failed(row["id"])
                    self.stats.failed += 1
                    log.warning("Durable observation failed handler=%s", row["handler_id"])
        finally:
            if current is not None:
                self._durable_tasks.discard(current)

    async def aclose(self, timeout: float = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        durable = [task for task in self._durable_tasks if not task.done()]
        for task in durable:
            task.cancel()
        if durable:
            await asyncio.gather(*durable, return_exceptions=True)
        workers = [task for task in self._workers if not task.done()]
        if self._queue is not None and workers:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError):
                log.warning("Observation queue did not drain within %.1fs; dropping %d", timeout, self._queue.qsize())
            except asyncio.CancelledError:
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
