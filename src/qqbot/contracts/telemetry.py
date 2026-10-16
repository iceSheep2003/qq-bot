"""Bounded operational evidence; never prompts or private model reasoning.

Task identity matters: asyncio maintenance tasks inherit ContextVars, but their
work is not part of the interactive turn that created them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol

log = logging.getLogger(__name__)


class TraceSink(Protocol):
    def save(self, trace: dict) -> None: ...


_active: ContextVar[dict | None] = ContextVar("operational_trace", default=None)
_FIELDS = frozenset({
    "allowed", "outcome", "reason", "skills", "memory_count", "wiki_pages",
    "wiki_mode", "tool", "error_kind", "prompt_tokens", "completion_tokens",
    "cached_tokens", "cache_write_tokens", "prefix_hash", "attempts",
    "duration_ms", "queue_ms", "model", "tools_offered", "route",
})


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def record(stage: str, **data) -> None:
    state = _active.get()
    if state is None or state["task"] is not _task() or len(state["trace"]["steps"]) >= 128:
        return
    step = {"stage": stage[:64], "at_ms": round((time.monotonic() - state["start"]) * 1000, 2)}
    for key, value in data.items():
        if key not in _FIELDS:
            continue
        if isinstance(value, str):
            value = value[:160]
        elif isinstance(value, (list, tuple)):
            value = [str(v)[:100] for v in value[:24]]
        elif value is not None and not isinstance(value, (int, float, bool)):
            continue
        step[key] = value
    state["trace"]["steps"].append(step)
    if "outcome" in step:
        state["trace"]["outcome"] = step["outcome"]


@contextmanager
def trace_turn(event, sink: TraceSink | None):
    if sink is None:
        yield
        return
    trace = {
        "event_id": str(event.event_id)[:160], "scope": str(event.scope)[:160],
        "user_id": str(event.user_id)[:100], "created_at": time.time(),
        "origin": str(getattr(event, "origin", "message"))[:32],
        "outcome": "completed", "steps": [],
    }
    state = {"trace": trace, "task": _task(), "start": time.monotonic()}
    token = _active.set(state)
    try:
        yield trace
    except BaseException as error:
        record("failure", outcome="cancelled" if isinstance(error, asyncio.CancelledError) else "error", error_kind=type(error).__name__)
        raise
    finally:
        trace["duration_ms"] = round((time.monotonic() - state["start"]) * 1000, 2)
        _active.reset(token)
        try:
            sink.save(trace)
        except Exception:
            log.warning("Operational trace could not be persisted")
