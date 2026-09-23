"""Scheduled action contracts and explicit registry.

Bootstrap registers only locally enabled feature packages. No file scan or
chat command can install executable code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Protocol

from ..domain import JobSkipped, MessageEvent
from ..ports import (
    ActivityRepository,
    AgentRunner,
    ConversationRepository,
    MessageSender,
)
from .spec import JobSpec

if TYPE_CHECKING:
    from ..runtime.service import BotPolicy

log = logging.getLogger(__name__)

DEFAULT_ACTION = "chat"


@dataclass(frozen=True)
class JobSuggestion:
    """A schedule a handler ships with, registered disabled.

    No group id: the suggestion is expanded over every allowlisted group.
    """

    id: str
    kind: str  # at | every | cron
    value: str
    prompt: str
    description: str = ""


def suggestion(
    job_id: str, value: str, prompt: str, *, kind: str = "cron", description: str = ""
) -> JobSuggestion:
    return JobSuggestion(job_id, kind, value, prompt, description)


class JobRuntime(Protocol):
    """The runtime capabilities that job handlers may use.

    This protocol intentionally contains no feature-specific renderer.
    """

    policy: BotPolicy
    agent: AgentRunner
    conversations: ConversationRepository
    activity: ActivityRepository
    sender: MessageSender
    last_reply: dict[str, float]

    def scope_lock(self, scope: str) -> asyncio.Lock: ...
    async def send_reply(
        self, group_id: str | None, user_id: str | None, text: str
    ) -> str: ...
    def today_start(self) -> int: ...
    def local_today(self) -> date: ...
    def job_event(self, job: dict, now: int) -> MessageEvent: ...
    def job_spec(self, job: dict) -> JobSpec: ...


class JobHandler(Protocol):
    """One kind of scheduled post.

    Raise JobSkipped for an expected no-op (quiet hours, nobody talking). The
    scheduler records that as ``skipped``; any other exception is a ``failed``.

    A handler with real parameters declares an optional ``validate_payload``
    that raises ``ValueError`` for a payload it cannot run with. It is called
    at startup, so a bad payload names the job instead of failing at 7am.
    """

    action: str

    def suggested_jobs(self) -> list[JobSuggestion]: ...
    async def run(self, bot: JobRuntime, job: dict) -> None: ...


class JobHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, instance: JobHandler) -> JobHandler:
        if instance.action in self._handlers:
            raise ValueError(f"duplicate job action: {instance.action}")
        self._handlers[instance.action] = instance
        return instance

    def get(self, action: str) -> JobHandler:
        found = self._handlers.get(action)
        if found is None:
            # Startup validation should have caught this; stay quiet in the
            # group rather than raising at 7am.
            log.error("No job handler registered for action %r", action)
            raise JobSkipped(f"unknown job action {action!r}")
        return found

    def actions(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def suggested_jobs(self) -> list[tuple[str, JobSuggestion]]:
        """(action, suggestion) for every schedule the handlers ship with."""
        found: list[tuple[str, JobSuggestion]] = []
        for action, instance in self._handlers.items():
            for item in getattr(instance, "suggested_jobs", list)():
                found.append((action, item))
        return found

    def payload_validators(
        self,
    ) -> dict[str, Callable[[Mapping[str, Any]], None]]:
        """Per-action payload validation, for the scheduler to call at startup.

        Only handlers that declare ``validate_payload`` appear here; the
        scheduler validates the JSON envelope for everyone else.
        """
        found: dict[str, Callable[[Mapping[str, Any]], None]] = {}
        for action, instance in self._handlers.items():
            check = getattr(instance, "validate_payload", None)
            if callable(check):
                found[action] = check
        return found
