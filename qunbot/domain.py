"""Framework-independent domain values.

This module holds the *stable shapes* the rest of the bot passes around: the
inbound event, the policies that govern a scope, and the typed dictionaries the
ports hand back. It imports nothing from the package, so every layer can depend
on it.

The dictionary types here describe payloads that cross a database or an HTTP
boundary, where a plain ``dict`` used to be the only contract. They are
``TypedDict`` on purpose: the runtime values are still real dicts (SQLite rows
and OpenAI-compatible JSON), so annotating them costs nothing and cannot break a
duck-typed implementer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, NotRequired, Protocol, TypedDict


class ConfigError(ValueError):
    """The deployment configuration is missing or internally inconsistent.

    Raised at startup, never mid-conversation: a bot that cannot say who it is
    allowed to talk to should not connect at all.
    """


class JobSkipped(Exception):
    """A scheduled run declined to post. Expected, not an error.

    Raised for quiet hours, exhausted quota or a group that went quiet, so the
    scheduler records ``skipped`` instead of ``failed`` and stops logging noise.
    """


@dataclass(frozen=True)
class MessageEvent:
    event_id: str
    scope: str
    group_id: str | None
    user_id: str
    nickname: str
    text: str
    image_urls: tuple[str, ...]
    at_bot: bool
    at_users: tuple[str, ...]
    timestamp: int
    # The group card ("群名片") when the platform supplied one. Distinct from
    # ``nickname``: a person can set a different card per group, so this is
    # stored per group and never promoted to the global profile.
    card: str = ""


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ConversationPolicy:
    """How the bot conducts a conversation.

    Nothing in here is about *when* the bot may speak unprompted; that is
    :class:`SchedulePolicy`. Keeping the two apart means a deployment can change
    its quiet hours without touching who it may answer, and a test that only
    exercises replies does not have to invent job quotas.
    """

    allowed_groups: frozenset[str]
    memory_extract_every: int
    timezone: str
    affection_auto_enabled: bool
    private_enabled: bool = False


@dataclass(frozen=True)
class SchedulePolicy:
    """When the bot is allowed to post on its own initiative.

    Active hours are wall-clock hours in the deployment timezone, which lives on
    :class:`ConversationPolicy`. There is deliberately exactly one clock: the
    earlier flat policy carried a single ``timezone`` used for both, and giving
    each policy its own copy would let them silently diverge.
    """

    # Scheduled posts have their own quota; optional proactive chat owns its own.
    daily_limit: int = 6
    active_start_hour: int = 7
    active_end_hour: int = 23
    cooldown_minutes: int = 30
    # "Group is not dead", not "someone spoke a minute ago" — a study group is
    # often silent for a while and a scheduled post should still land.
    freshness_minutes: int = 180
    max_chars: int = 150


@dataclass(frozen=True)
class BotPolicy:
    """The bot's operating policy, composed of its two halves.

    ``runtime.service`` and ``scheduling.guards`` read these values by their
    flat legacy names, so the composite exposes them as properties rather than
    forcing a change to modules this work does not own. See the delivery report
    for the one-line wiring that removes the duplicate flat dataclass still in
    ``qunbot/runtime/service.py``.
    """

    conversation: ConversationPolicy
    schedule: SchedulePolicy

    # --- conversation ---------------------------------------------------
    @property
    def allowed_groups(self) -> frozenset[str]:
        return self.conversation.allowed_groups

    @property
    def memory_extract_every(self) -> int:
        return self.conversation.memory_extract_every

    @property
    def timezone(self) -> str:
        return self.conversation.timezone

    @property
    def affection_auto_enabled(self) -> bool:
        return self.conversation.affection_auto_enabled

    @property
    def private_enabled(self) -> bool:
        return self.conversation.private_enabled

    # --- schedule -------------------------------------------------------
    @property
    def job_daily_limit(self) -> int:
        return self.schedule.daily_limit

    @property
    def active_start_hour(self) -> int:
        return self.schedule.active_start_hour

    @property
    def active_end_hour(self) -> int:
        return self.schedule.active_end_hour

    @property
    def job_cooldown_minutes(self) -> int:
        return self.schedule.cooldown_minutes

    @property
    def job_freshness_minutes(self) -> int:
        return self.schedule.freshness_minutes

    @property
    def job_max_chars(self) -> int:
        return self.schedule.max_chars


# --------------------------------------------------------------------------- #
# Wire and row shapes
# --------------------------------------------------------------------------- #
Role = Literal["user", "assistant", "system", "tool"]
TrustLabel = Literal["low", "medium", "high"]
JobStatus = Literal["ok", "skipped", "failed"]


class ChatMessage(TypedDict):
    """One OpenAI-compatible chat message, as sent to and returned by a model."""

    role: str
    content: str | None
    name: NotRequired[str]
    tool_calls: NotRequired[list[dict[str, Any]]]
    tool_call_id: NotRequired[str]


class ModelUsage(TypedDict, total=False):
    """Token accounting as the provider reported it.

    ``total=False`` and not ``0``: a missing ``cached_tokens`` means "the
    provider did not say", which is a different fact from "no cache hits".
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_tokens: int | str


class ModelChoice(TypedDict):
    message: ChatMessage
    finish_reason: NotRequired[str]


class ModelResult(TypedDict):
    """The normalised completion. ``usage`` is absent when the provider omits it."""

    choices: list[ModelChoice]
    usage: NotRequired[ModelUsage]


class ConversationRow(TypedDict):
    """A stored transcript row, as ``ConversationRepository.recent`` returns it."""

    id: int
    event_id: str
    scope: str
    user_id: str
    nickname: str
    role: str
    content: str
    created_at: int


class JobRow(TypedDict):
    """A row of the ``jobs`` table. Mirrors the schema, which ``SELECT *`` fixes."""

    id: int
    group_id: str
    schedule_kind: str
    schedule_value: str
    prompt: str
    enabled: int
    next_run: int
    created_by: str
    created_at: int
    config_key: str | None
    action: str


class JobRunResult(TypedDict):
    """The outcome written for one reserved run of a job."""

    job_id: int
    status: JobStatus
    detail: str
    started_at: int
    finished_at: int | None


class SendReceipt(TypedDict, total=False):
    """What a ``MessageSender`` gets back: the OneBot response ``data`` object."""

    message_id: int


class SkillRef(Protocol):
    """The part of a selected skill the prompt builder reads."""

    name: str
    description: str
    body: str


class ContextFragment(Protocol):
    """One provider's contribution to the dynamic prompt suffix.

    Structurally satisfied by ``runtime.context.ContextContribution``. Stated
    here so a consumer outside that module can name the shape it receives
    without importing the registry.
    """

    name: str
    payload: Any
    # A ``Trust`` IntEnum; typed as int because the enum lives in the runtime.
    trust: int
    priority: int
    max_chars: int
    scope: str
    failed: bool

    def rendered(self) -> Any: ...
