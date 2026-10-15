"""Own the asynchronous memory work around a conversation turn.

The interactive service supplies persistence/model ports and chooses the
successful admission and delivery boundaries. This component owns task state,
scope reservations, concurrency and cancellation; it never sends messages.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from qqbot.agent.fairness import ScopeWorkBudget
from qqbot.contracts.models import MessageEvent
from qqbot.contracts.ports import ConversationCompactionPort


class ConversationMaintenance:
    def __init__(
        self,
        *,
        message_count: Callable[[str], int],
        extract_memory: Callable[[str], Awaitable[None]],
        extract_every: int,
        compactor: ConversationCompactionPort | None = None,
        extraction_concurrency: int = 2,
        compaction_budget: ScopeWorkBudget | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if extract_every < 1 or extraction_concurrency < 1:
            raise ValueError("maintenance interval and concurrency must be positive")
        self._message_count = message_count
        self._extract_memory = extract_memory
        self._extract_every = extract_every
        self._compactor = compactor
        self._extract_slots = asyncio.Semaphore(extraction_concurrency)
        self._compaction_budget = (
            compaction_budget if compaction_budget is not None else ScopeWorkBudget(max_per_scope=2)
        )
        self._log = logger if logger is not None else logging.getLogger(__name__)
        self._extractions: dict[str, asyncio.Task] = {}
        self._compactions: dict[str, asyncio.Task] = {}
        self._closed = False
        self._shutdown: asyncio.Task | None = None

    def maybe_extract(self, scope: str) -> bool:
        """Admit one extraction at a positive persisted-message interval."""
        if self._closed or scope in self._extractions:
            return False
        count = self._message_count(scope)
        if count <= 0 or count % self._extract_every:
            return False
        self._start(self._extractions, scope, self._extract(scope))
        return True

    def after_reply(self, event: MessageEvent) -> bool:
        """Admit compaction after the caller has committed actual delivery."""
        if (
            self._closed
            or event.origin == "operator"
            or self._compactor is None
            or event.scope in self._compactions
            or not self._compaction_budget.admit(event.scope)
        ):
            return False
        self._start(self._compactions, event.scope, self._compact(event))
        return True

    @staticmethod
    def _start(tasks: dict[str, asyncio.Task], scope: str, work) -> None:
        task = asyncio.create_task(work)
        tasks[scope] = task

        def release(done: asyncio.Task) -> None:
            # A late callback must never remove a replacement task's reservation.
            if tasks.get(scope) is done:
                del tasks[scope]

        task.add_done_callback(release)

    async def _extract(self, scope: str) -> None:
        try:
            async with self._extract_slots:
                await self._extract_memory(scope)
        except Exception:
            self._log.exception("Memory extraction failed for %s", scope)

    async def _compact(self, event: MessageEvent) -> None:
        try:
            await self._compactor.compact_if_needed(
                event.scope, current_event_id=event.event_id
            )
        except Exception:
            self._log.exception("Background short-term compaction failed scope=%s", event.scope)

    @property
    def pending_extractions(self) -> frozenset[str]:
        return frozenset(self._extractions)

    @property
    def pending_compactions(self) -> frozenset[str]:
        return frozenset(self._compactions)

    async def aclose(self) -> None:
        """Refuse new work, cancel all jobs and settle cleanup before returning.

        Each closer awaits the same shutdown task. Shielding it prevents a
        cancelled caller from interrupting another job's cleanup or allowing
        storage teardown to race that cleanup.
        """
        if self._shutdown is None:
            self._closed = True
            tasks = [*self._extractions.values(), *self._compactions.values()]
            for task in tasks:
                task.cancel()
            self._shutdown = asyncio.create_task(self._settle(tasks))
        cancelled: asyncio.CancelledError | None = None
        while True:
            try:
                await asyncio.shield(self._shutdown)
                break
            except asyncio.CancelledError as error:
                # Further cancellation requests must not release the caller
                # while a job is still unwinding against live storage.
                cancelled = error
        if cancelled is not None:
            raise cancelled

    async def _settle(self, tasks: list[asyncio.Task]) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        self._extractions.clear()
        self._compactions.clear()
