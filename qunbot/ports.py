"""Application-facing contracts. Infrastructure adapters implement these ports.

No OneBot, SQLite or HTTP client types may appear in this module.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from .domain import MessageEvent


class ReplyResult(Protocol):
    text: str


class AgentRunner(Protocol):
    async def reply(
        self, event: MessageEvent, *, proactive: bool = False
    ) -> ReplyResult: ...
    async def extract_memory(self, scope: str) -> None: ...


class AffectionObserver(Protocol):
    async def observe(self, event: MessageEvent, bot_reply: str) -> None: ...


class ConversationRepository(Protocol):
    def add_message(
        self,
        event_id: str,
        scope: str,
        user_id: str,
        nickname: str,
        role: str,
        content: str,
    ) -> bool: ...
    def recent(self, scope: str, limit: int = 24) -> list[dict[str, Any]]: ...
    def message_count(self, scope: str) -> int: ...


class PeopleRepository(Protocol):
    def profile(self, group_id: str, user_id: str) -> dict[str, Any]: ...
    def change_affection(
        self, group_id: str, user_id: str, delta: int, reason: str
    ) -> int: ...


class MemoryRepository(Protocol):
    def search_memories(
        self, scope: str, query: str, limit: int = 5
    ) -> list[dict[str, Any]]: ...
    def remember(
        self,
        scope: str,
        user_id: str,
        content: str,
        *,
        importance: int = 1,
        source_event_id: str | None = None,
    ) -> int: ...
    def has_memory(self, scope: str, user_id: str, content: str) -> bool: ...


class ActivityRepository(Protocol):
    def proactive_count_since(
        self, group_id: str, since: int, source: str | None = None
    ) -> int: ...
    def last_proactive(self, group_id: str) -> int: ...
    def log_proactive(
        self, group_id: str, content: str, source: str = "random"
    ) -> None: ...


class JobRepository(Protocol):
    def replace_config_jobs(
        self, jobs: list[tuple[str, str, str, str, str, str, int]], now: int
    ) -> None: ...
    def list_jobs(self, group_id: str) -> list[dict[str, Any]]: ...
    def disable_job(self, group_id: str, job_id: int) -> bool: ...
    def due_jobs(self, now: int, limit: int = 10) -> list[dict[str, Any]]: ...
    def reserve_job(
        self, job: dict[str, Any], next_run: int | None, now: int
    ) -> int | None: ...
    def finish_job(self, run_id: int, status: str, detail: str, now: int) -> None: ...
    def reconcile_interrupted_jobs(self, now: int) -> None: ...
    def missed_jobs(self, before: int) -> list[dict[str, Any]]: ...
    def skip_job(self, job: dict[str, Any], next_run: int | None, now: int) -> None: ...


class ChatModel(Protocol):
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.7,
    ) -> dict: ...


class MessageSender(Protocol):
    async def send(
        self,
        *,
        group_id: str | None = None,
        user_id: str | None = None,
        text: str = "",
        at_user: str | None = None,
        image: str | None = None,
        voice: str | None = None,
    ) -> dict: ...


class MediaProcessor(Protocol):
    async def compose(self, text: str) -> tuple[str, str | None, str | None]: ...


class PosterRenderer(Protocol):
    def days_left(self, today: date) -> int: ...
    def render(self, today: date, motto: str = "") -> str | None: ...


class SkillProvider(Protocol):
    def catalog_text(self) -> str: ...
    def select(self, text: str, *, proactive: bool = False) -> list[Any]: ...


class ToolProvider(Protocol):
    def schemas(self) -> list[dict]: ...
    def call(self, name: str, args: dict[str, Any], event: Any) -> str: ...


class ContextProvider(Protocol):
    def collect(self, event: Any) -> dict[str, Any]: ...
