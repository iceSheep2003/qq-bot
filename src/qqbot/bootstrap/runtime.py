"""Application runtime and reverse-order resource cleanup."""
from __future__ import annotations
import asyncio
import logging
from inspect import isawaitable
from typing import Any, Callable
log = logging.getLogger("qqbot.bootstrap.app")

Closer = Callable[[], Any]
"""A teardown step. May be sync or return an awaitable."""


def run_awaitable(awaitable: Any) -> None:
    """Settle an async teardown from a synchronous context.

    A failed ``BotApp.__init__`` cannot await: it is called as
    ``asyncio.run(BotApp(config).run())``, so its constructor runs *outside*
    the loop. The usual case is no running loop at all, which ``asyncio.run``
    handles; if a caller did construct inside a loop, the step is scheduled and
    this is best-effort rather than silently dropped.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(awaitable)
        return
    asyncio.ensure_future(awaitable)


def close_sync(closers: list[Closer]) -> None:
    """Reverse-order teardown that never raises, for a constructor rollback."""
    for close in reversed(closers):
        try:
            result = close()
        except Exception:
            log.exception("Cleanup step failed while unwinding a failed assembly")
            continue
        if isawaitable(result):
            try:
                run_awaitable(result)
            except Exception:
                log.exception("Async cleanup step failed while unwinding")


async def close_all(closers: list[Closer]) -> None:
    """The same reverse-order teardown, awaited. Used by ``BotApp.run``."""
    for close in reversed(closers):
        try:
            result = close()
            if isawaitable(result):
                await result
        except Exception:
            log.exception("Cleanup step failed during shutdown")


class ApplicationRuntime:
    """Run deferred workers and settle them before releasing their resources."""

    def __init__(self, workers, closers):
        self.workers = tuple(workers)
        self.closers = tuple(closers)

    async def run(self) -> None:
        tasks = []
        try:
            for worker in self.workers:
                tasks.append(asyncio.ensure_future(worker()))
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await close_all(self.closers)
