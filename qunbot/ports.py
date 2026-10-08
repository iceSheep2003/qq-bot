"""Application-facing contracts. Infrastructure adapters implement these ports.

No OneBot, SQLite or HTTP client types may appear in this module.
"""

from __future__ import annotations

from typing import Any, Protocol

from .domain import (
    ChatMessage,
    ConversationRow,
    JobRow,
    MessageEvent,
    ModelResult,
    SendReceipt,
    SkillRef,
    TrustLabel,
)


class ReplyResult(Protocol):
    """What a turn produced as far as its callers are concerned.

    Only ``text`` is required. ``runtime.agent.Reply`` also carries usage and a
    prefix hash, but a test double or a future cheap agent need not, and the
    conversation service never reads them — so the contract stays at the one
    field every implementation can honour.
    """

    text: str


class ReplyPlanner(Protocol):
    async def plan(self, draft: Any, event: MessageEvent | None = None) -> Any: ...


class ReplyDelivery(Protocol):
    async def dispatch(
        self, plan: Any, *, group_id: str | None, user_id: str | None,
        allowed_at: frozenset[str] = frozenset(),
    ) -> Any: ...


class ReplyDecisionPolicy(Protocol):
    """Decides whether a turn warrants a reply at all.

    Async because reading the room may need the model. Returning ``False`` is a
    decision, not a failure: the message stays in the transcript and simply
    does not get answered. The default policy is "group messages only when
    @-ed"; a smarter one can abstain far more often.
    """

    async def decide(
        self, event: MessageEvent, *, recent: list[ConversationRow]
    ) -> bool: ...


class TurnGuard(Protocol):
    """Classify likely bait before a turn; it cannot mutate configuration."""

    async def decide(
        self, event: MessageEvent, *, recent: list[ConversationRow]
    ) -> Any: ...


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
    def recent(self, scope: str, limit: int = 24) -> list[ConversationRow]: ...
    def after_id(
        self, scope: str, message_id: int, limit: int = 100
    ) -> list[ConversationRow]: ...
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
    def related_context(self, scope: str, query: str, limit: int = 4) -> dict: ...
    async def extract(self, scope: str) -> None: ...
    def overview(self, scope: str | None = None) -> dict[str, Any]: ...
    def memory_detail(self, memory_id: int) -> dict[str, Any] | None: ...
    def topics(self, scope: str | None = None, limit: int = 100) -> list[dict]: ...
    def extraction_runs(
        self, scope: str | None = None, limit: int = 50
    ) -> list[dict]: ...


class ActivityRepository(Protocol):
    def proactive_count_since(
        self, group_id: str, since: int, source: str | None = None
    ) -> int: ...
    def last_proactive(self, group_id: str) -> int: ...
    def proactive_count_prefix_since(
        self, group_id: str, since: int, source_prefix: str
    ) -> int: ...
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
    def list_jobs(self, group_id: str) -> list[JobRow]: ...
    def disable_job(self, group_id: str, job_id: int) -> bool: ...
    def due_jobs(self, now: int, limit: int = 10) -> list[JobRow]: ...
    def reserve_job(
        self, job: JobRow, next_run: int | None, now: int
    ) -> int | None: ...
    def finish_job(self, run_id: int, status: str, detail: str, now: int) -> None: ...
    def reconcile_interrupted_jobs(self, now: int) -> None: ...
    def missed_jobs(self, before: int) -> list[JobRow]: ...
    def skip_job(self, job: JobRow, next_run: int | None, now: int) -> None: ...


class ChatModel(Protocol):
    """The provider edge. Implementations return the OpenAI-compatible shape.

    ``ModelResult`` is a ``TypedDict``, so an adapter that returns a plain dict
    still satisfies this port — the annotation only names the fields callers
    are allowed to rely on.
    """

    async def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.7,
    ) -> ModelResult: ...


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
        qq_face: str | None = None,
        reply_to: str | None = None,
    ) -> SendReceipt: ...


class MessageReactionSender(Protocol):
    async def react_to_message(
        self, message_id: str, emoji_id: str, *, set_reaction: bool = True
    ) -> Any: ...


class MessageReactionPolicy(Protocol):
    def decide(self, event: MessageEvent) -> Any: ...


class NativeInteractionSender(Protocol):
    async def send_native(self, group_id: str, kind: str) -> Any: ...
    async def poke_group(self, group_id: str, user_id: str) -> Any: ...


class LightInteractionPolicyPort(Protocol):
    def decide(self, event: MessageEvent, recent: list[dict]) -> Any: ...


class OutboundParts(Protocol):
    """A parsed, media-resolved reply — the shape ``MessageSender.send`` takes."""

    @property
    def text(self) -> str: ...
    def send_kwargs(self) -> dict[str, str]: ...


class MediaProcessor(Protocol):
    async def compose(self, text: str) -> tuple[str, str | None, str | None]: ...
    async def compose_message(
        self, text: str, *, allowed_at: frozenset[str] | None = None,
        voice_style: str = "neutral",
    ) -> OutboundParts: ...


class SkillProvider(Protocol):
    def catalog_text(self) -> str: ...
    def select(self, text: str, *, proactive: bool = False) -> list[SkillRef]: ...


class ToolProvider(Protocol):
    def schemas(self) -> list[dict]: ...
    def call(self, name: str, args: dict[str, Any], event: MessageEvent) -> str: ...


class ConversationIntelligencePort(Protocol):
    """Local, model-free group-topic analysis."""

    def observe(self, event: MessageEvent) -> None: ...
    def frame(self, event: MessageEvent) -> Any: ...
    def latest_frame(self, scope: str) -> Any | None: ...


class PromptSessionRepository(Protocol):
    def load(self, scope: str, limit: int = 1000) -> list[dict]: ...
    def append(self, scope: str, messages: list[dict]) -> None: ...
    def clear(self, scope: str) -> int: ...
    def compaction_candidate(
        self, scope: str, *, max_messages: int = 80,
        max_chars: int = 32000, tail_messages: int = 24,
    ) -> dict[str, Any] | None: ...
    def replace_compacted(
        self, scope: str, *, expected_sequence: int,
        snapshot: dict, tail: list[dict],
    ) -> bool: ...


class ConversationCompactionPort(Protocol):
    async def compact_if_needed(
        self, scope: str, current_event_id: str = ""
    ) -> bool: ...


class TopicObservationRepository(Protocol):
    def add(self, event: MessageEvent) -> None: ...
    def recent(self, scope: str, limit: int = 200) -> list[MessageEvent]: ...


class ContextProvider(Protocol):
    """The dynamic prompt suffix.

    ``collect_with_trust`` maps contributor name to payload, plus the authority
    label for exactly those contributors that produced something. The two are
    returned together because they must agree: an authority note naming a
    contribution the model was not given is noise, and one missing a
    contribution it *was* given is a misleading label on untrusted text.

    Payloads stay ``Any`` on purpose: a contributor may legitimately return a
    string, a mapping or a list, and the registry — not this port — owns the
    budget, priority and truncation that make them safe to concatenate.

    ``collect`` and ``trust_map`` are convenience halves of the same selection,
    for callers that need only one side; each runs the providers once.
    """

    def collect_with_trust(
        self, event: MessageEvent
    ) -> tuple[dict[str, Any], dict[str, TrustLabel]]: ...
    def collect(self, event: MessageEvent) -> dict[str, Any]: ...
    def trust_map(self, event: MessageEvent) -> dict[str, TrustLabel]: ...
