"""Application-facing contracts. Infrastructure adapters implement these ports.

No OneBot, SQLite or HTTP client types may appear in this module.
"""

from __future__ import annotations

from typing import Any, Protocol

from .domain import MessageEvent


class ReplyResult(Protocol):
    text: str


class ReplyDecisionPolicy(Protocol):
    """Decides whether a turn warrants a reply at all.

    Async because reading the room may need the model. Returning ``False`` is a
    decision, not a failure: the message stays in the transcript and simply
    does not get answered. The default policy is "group messages only when
    @-ed"; a smarter one can abstain far more often.
    """

    async def decide(
        self, event: MessageEvent, *, recent: list[dict[str, Any]]
    ) -> bool: ...


class AgentRunner(Protocol):
    async def reply(
        self, event: MessageEvent, *, proactive: bool = False
    ) -> ReplyResult: ...
    async def extract_memory(self, scope: str) -> None: ...


class AffectionObserver(Protocol):
    async def observe(self, event: MessageEvent, bot_reply: str) -> None: ...


class ReplyObserver(Protocol):
    async def observe(self, event: MessageEvent, bot_reply: str) -> None: ...


class MoodObserver(ReplyObserver, Protocol):
    """The bot's own affect. Implemented by ``qunbot.emotion``.

    ``observe`` is the post-reply assessment; ``permits_proactive`` is the
    "not in the mood to start a conversation" gate. Scheduled jobs are never
    gated — those are explicitly requested by the deployer.
    """

    def permits_proactive(self, scope: str) -> bool: ...


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
    def observe_user(self, user_id: str, nickname: str) -> None: ...
    def observe_group_member(
        self, group_id: str, user_id: str, nickname: str, card: str = ""
    ) -> None: ...
    def profile(self, group_id: str, user_id: str) -> dict[str, Any]: ...
    def change_affection(
        self, group_id: str, user_id: str, delta: int, reason: str
    ) -> int: ...


class MemoryRepository(Protocol):
    def search_memories(
        self, scope: str, query: str, limit: int = 5, **kwargs: Any
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


class MemoryIndex(Protocol):
    """The write and lifecycle surface ``MemoryService`` drives.

    Split from ``MemoryRepository`` on purpose: reading memories is a different
    capability from managing their lifecycle, and a deployment that only needs
    the read path can implement the smaller protocol. ``MemoryService`` calls
    these duck-typed and degrades to lexical-only recall when they are missing.
    """

    def attach_embedder(self, embedder: Any) -> Any: ...
    def observe(
        self,
        scope: str,
        subject_user_id: str,
        content: str,
        *,
        fact_type: str = ...,
        confidence: float = ...,
        importance: int = ...,
        source_event_id: str | None = ...,
        origin_user_id: str | None = ...,
        visibility: str = ...,
        expires_at: int | None = ...,
        supersede: bool = ...,
        now: int | None = ...,
        **kwargs: Any,
    ) -> dict[str, Any]: ...
    def reinforce(self, memory_ids: Any, now: int | None = None) -> int: ...
    def similar_candidates(
        self, scope: str, subject_user_id: str, *, limit: int = 60
    ) -> list[dict[str, Any]]: ...
    def candidates(
        self,
        scope: str,
        query: str,
        *,
        subject_user_id: str | None = ...,
        **kwargs: Any,
    ) -> list[dict[str, Any]]: ...
    def maintain(self, now: int | None = None) -> dict[str, Any]: ...
    def forget(self, memory_ids: Any) -> int: ...
    def forget_subject(self, scope: str, user_id: str) -> int: ...
    def forget_scope(self, scope: str) -> int: ...
    def stats(self, scope: str | None = None) -> dict[str, Any]: ...


class MemoryCoordinator(Protocol):
    def related(self, scope: str, query: str, limit: int = 4) -> list[str]: ...
    async def extract(self, scope: str) -> None: ...


class ActivityRepository(Protocol):
    def proactive_count_since(
        self, group_id: str, since: int, source: str | None = None
    ) -> int: ...
    def last_proactive(self, group_id: str) -> int: ...
    def log_proactive(
        self, group_id: str, content: str, source: str = "random"
    ) -> None: ...


class JobRepository(Protocol):
    def sync_jobs(
        self,
        configured: list[tuple[str, str, str, str, str, str, int]],
        suggested: list[tuple[str, str, str, str, str, str, int]],
        now: int,
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


class SkillProvider(Protocol):
    def catalog_text(self) -> str: ...
    def select(self, text: str, *, proactive: bool = False) -> list[Any]: ...


class ToolProvider(Protocol):
    def schemas(self) -> list[dict]: ...
    def call(self, name: str, args: dict[str, Any], event: Any) -> str: ...


class ContextProvider(Protocol):
    def collect(self, event: Any) -> dict[str, Any]: ...
    def trust_map(self, event: Any) -> dict[str, str]: ...
