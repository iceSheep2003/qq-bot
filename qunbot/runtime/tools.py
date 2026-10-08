"""Model-visible tools: declared permissions, per-minute quota, audit trail.

A tool is the one place a model can make the bot *do* something rather than say
something, so this registry is where the deployer's authority over that is
enforced — not in a prompt, and not in each handler.

Three properties are structural rather than configurable:

``permissions``  every tool declares what it touches. A tool that asks for a
                 privileged permission (writing affection, editing the stable
                 persona, creating jobs, arbitrary network or filesystem
                 access) is **refused at registration**. Those are not
                 deployer-tunable knobs: a group member cannot reach them
                 through chat, and an extension cannot reach them through the
                 tool registry either.
``quota``        each tool is capped per minute and per conversation scope, so
                 a model that loops on one tool cannot turn one reply into an
                 unbounded stream of calls. Exceeding the quota is a *refusal
                 the model sees*, not an exception.
``audit``        every call — allowed or refused, known or unknown — appends a
                 bounded record naming the tool, scope, speaker, source
                 extension and reason. The last ``audit_size`` entries are kept
                 in memory and readable via :meth:`ToolRegistry.audit_log`.

Arguments are validated against the tool's own JSON schema before the handler
runs; an invalid call raises ``ValueError``, which the agent loop already turns
into a tool-role error message for the model. Keys the schema does not mention
are ignored rather than rejected — models add noise keys, and refusing a useful
call over a stray key would be worse than ignoring it.
"""

from __future__ import annotations

import enum
import inspect
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..domain import MessageEvent
from ..ports import MemoryRepository

log = logging.getLogger(__name__)

#: Default cap, per tool, per conversation scope. Kept in step with
#: ``qunbot.extensions.manifest.DEFAULT_TOOL_QUOTA_PER_MINUTE``; a contract test
#: pins the two together so a manifest and the runtime cannot drift.
DEFAULT_TOOL_QUOTA_PER_MINUTE = 20
QUOTA_WINDOW_SECONDS = 60.0

#: How many audit records are retained. Bounded: this is an operational trail,
#: not a data store, and an unbounded log in a long-running bot is a leak.
AUDIT_LOG_SIZE = 256

#: Serialized-argument ceiling. A model that stuffs a document into a tool call
#: is not asking a question; it is moving text around outside the conversation.
MAX_ARGS_CHARS = 4096


class ToolPermission(enum.Flag):
    """What a tool is allowed to touch.

    Grantable permissions are read-only or conversational. Privileged ones are
    refused outright by :meth:`ToolRegistry.register` — there is no environment
    variable or manifest flag that turns them on, because "the deployer could
    enable it" is exactly the door group chat must not be behind.
    """

    NONE = 0
    READ_MEMORY = enum.auto()        # read the current scope's long-term memory
    READ_CONVERSATION = enum.auto()  # read the current scope's transcript
    SEND_MESSAGE = enum.auto()       # produce something the bot will send
    READ_GROUP = enum.auto()         # read current-group metadata/files
    MODERATE_GROUP = enum.auto()     # bounded OneBot moderation via policy gate

    # Privileged: never grantable to an extension tool.
    MODIFY_AFFECTION = enum.auto()   # write a group member's relationship score
    MODIFY_PERSONA = enum.auto()     # write the stable persona or a strategy
    CREATE_JOB = enum.auto()         # create, enable or disable a schedule
    NETWORK = enum.auto()            # arbitrary outbound HTTP
    FILESYSTEM = enum.auto()         # arbitrary filesystem access

    @classmethod
    def grantable(cls) -> ToolPermission:
        return (
            cls.READ_MEMORY | cls.READ_CONVERSATION | cls.SEND_MESSAGE
            | cls.READ_GROUP | cls.MODERATE_GROUP
        )

    @classmethod
    def privileged(cls) -> ToolPermission:
        return (
            cls.MODIFY_AFFECTION
            | cls.MODIFY_PERSONA
            | cls.CREATE_JOB
            | cls.NETWORK
            | cls.FILESYSTEM
        )


def describe_permissions(permissions: ToolPermission) -> list[str]:
    """Stable, sorted names for a permission set (errors, audits, --check)."""
    permissions = ToolPermission(permissions or ToolPermission.NONE)
    return sorted(
        member.name.lower()
        for member in ToolPermission
        if member.value and (member & permissions)
    )


@dataclass(frozen=True)
class ToolAuditEntry:
    """One call attempt, successful or not."""

    timestamp: float
    tool: str
    scope: str
    user_id: str
    source: str
    allowed: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "ts": round(self.timestamp, 3),
            "tool": self.tool,
            "scope": self.scope,
            "user_id": self.user_id,
            "source": self.source,
            "allowed": self.allowed,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Tool:
    """One callable the model may ask for.

    ``source`` names the extension that registered it (``"core"`` for the
    built-ins) so the audit trail answers "who exposed this?" as well as "who
    called it?".
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], MessageEvent], str]
    permissions: ToolPermission = ToolPermission.NONE
    quota_per_minute: int = DEFAULT_TOOL_QUOTA_PER_MINUTE
    source: str = "core"

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def permission_names(self) -> list[str]:
        return describe_permissions(self.permissions)


def _validate_arguments(tool: Tool, args: Any) -> None:
    """Reject a call the tool's own schema does not accept.

    Raises ``ValueError`` so the agent loop reports it back to the model as a
    tool error instead of executing anything.
    """
    if not isinstance(args, dict):
        raise ValueError(f"tool {tool.name} expects an object of arguments")
    try:
        encoded = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raise ValueError(f"tool {tool.name} received unserializable arguments")
    if len(encoded) > MAX_ARGS_CHARS:
        raise ValueError(
            f"tool {tool.name} arguments exceed {MAX_ARGS_CHARS} characters"
        )

    schema = tool.parameters or {}
    kind = schema.get("type")
    if kind not in (None, "object"):
        raise ValueError(f"tool {tool.name} declares a non-object schema")
    missing = [key for key in schema.get("required") or () if key not in args]
    if missing:
        raise ValueError(
            f"tool {tool.name} is missing required argument(s): {sorted(missing)}"
        )
    expected_by_name = {
        key: spec.get("type")
        for key, spec in (schema.get("properties") or {}).items()
        if isinstance(spec, dict)
    }
    for key, value in args.items():
        expected = expected_by_name.get(key)
        if expected is None:
            continue  # unknown keys are model noise, not a reason to refuse
        if not _matches_type(value, expected):
            raise ValueError(f"tool {tool.name} argument {key!r} should be {expected}")


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True  # an unknown JSON type in a schema is not the model's fault


class ToolRegistry:
    """Model-visible capabilities registered outside the agent loop.

    ``clock`` is injectable so quota behaviour is testable without sleeping;
    it must be monotonic.
    """

    def __init__(
        self,
        *,
        audit_size: int = AUDIT_LOG_SIZE,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: float = QUOTA_WINDOW_SECONDS,
    ):
        self._tools: dict[str, Tool] = {}
        self._calls: dict[tuple[str, str], deque[float]] = {}
        self._audit: deque[ToolAuditEntry] = deque(maxlen=max(1, int(audit_size)))
        self._clock = clock
        self._window = max(1.0, float(window_seconds))

    # -- registration -------------------------------------------------------

    def register(self, tool: Tool, *, source: str | None = None) -> Tool:
        """Add a tool, or refuse it.

        Refusals are startup errors, not runtime ones: a tool with a privileged
        permission or a duplicate name never reaches the model at all.
        """
        if not tool.name or not tool.name.strip():
            raise ValueError("tool needs a non-empty name")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        privileged = tool.permissions & ToolPermission.privileged()
        if privileged:
            raise ValueError(
                f"tool {tool.name!r} requests privileged permission(s) "
                f"{describe_permissions(privileged)}; extensions may only hold "
                f"{describe_permissions(ToolPermission.grantable())}"
            )
        undeclared = tool.permissions & ~ToolPermission.grantable()
        if undeclared:  # unreachable via the check above; kept as a hard stop
            raise ValueError(
                f"tool {tool.name!r} requests unknown permission(s) "
                f"{describe_permissions(undeclared)}"
            )
        if int(tool.quota_per_minute) < 1:
            raise ValueError(f"tool {tool.name!r} declares a quota below 1")
        if source is not None:
            tool = Tool(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
                handler=tool.handler,
                permissions=tool.permissions,
                quota_per_minute=tool.quota_per_minute,
                source=source,
            )
        self._tools[tool.name] = tool
        return tool

    # -- inspection ---------------------------------------------------------

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def permissions(self, name: str) -> list[str]:
        tool = self._tools.get(name)
        return tool.permission_names() if tool else []

    def quota_per_minute(self, name: str) -> int:
        tool = self._tools.get(name)
        return tool.quota_per_minute if tool else 0

    def audit_log(self) -> tuple[ToolAuditEntry, ...]:
        """The retained call trail, oldest first."""
        return tuple(self._audit)

    # -- calling ------------------------------------------------------------

    def call(self, name: str, args: dict[str, Any], event: MessageEvent) -> str:
        """Run a tool, or return the model-readable reason it did not run."""
        tool = self._tools.get(name)
        if tool is None:
            # Kept as a string rather than an exception: a cheerful error for a
            # model that hallucinated a tool name, and cheap to log.
            self._record(name, "unknown", False, "unknown tool", event)
            return "unknown tool"
        try:
            _validate_arguments(tool, args)
        except ValueError as exc:
            self._record(tool.name, tool.source, False, str(exc), event)
            raise
        remaining, retry_after = self._spend(tool, event.scope)
        if remaining < 0:
            self._record(
                tool.name, tool.source, False, "quota exceeded", event
            )
            log.warning(
                "Tool %s quota exceeded (source=%s scope=%s user=%s)",
                tool.name,
                tool.source,
                event.scope,
                event.user_id,
            )
            return (
                f"tool {tool.name} quota exceeded "
                f"({tool.quota_per_minute}/min in this conversation); "
                f"retry in {retry_after:.0f}s"
            )
        self._record(tool.name, tool.source, True, "ok", event)
        log.info(
            "Tool call %s (source=%s scope=%s user=%s)",
            tool.name,
            tool.source,
            event.scope,
            event.user_id,
        )
        result = tool.handler(args, event)
        if inspect.isawaitable(result):
            raise RuntimeError(
                f"tool {tool.name} is asynchronous; use ToolRegistry.acall"
            )
        return str(result)

    async def acall(self, name: str, args: dict[str, Any], event: MessageEvent) -> str:
        """Async-capable twin of :meth:`call` for platform-backed tools."""
        tool = self._tools.get(name)
        if tool is None:
            self._record(name, "unknown", False, "unknown tool", event)
            return "unknown tool"
        try:
            _validate_arguments(tool, args)
        except ValueError as exc:
            self._record(tool.name, tool.source, False, str(exc), event)
            raise
        remaining, retry_after = self._spend(tool, event.scope)
        if remaining < 0:
            self._record(tool.name, tool.source, False, "quota exceeded", event)
            return (
                f"tool {tool.name} quota exceeded "
                f"({tool.quota_per_minute}/min in this conversation); "
                f"retry in {retry_after:.0f}s"
            )
        self._record(tool.name, tool.source, True, "ok", event)
        log.info(
            "Tool call %s (source=%s scope=%s user=%s)",
            tool.name, tool.source, event.scope, event.user_id,
        )
        result = tool.handler(args, event)
        if inspect.isawaitable(result):
            result = await result
        return str(result)

    def _spend(self, tool: Tool, scope: str) -> tuple[int, float]:
        """Consume one call from the window. Returns (remaining, retry_after).

        A negative remaining means the call was refused; the caller reports it
        to the model rather than raising, because a quota is not a bug.
        """
        now = self._clock()
        window = self._calls.setdefault((tool.name, scope), deque())
        cutoff = now - self._window
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= tool.quota_per_minute:
            return -1, max(0.0, window[0] + self._window - now)
        window.append(now)
        return tool.quota_per_minute - len(window), 0.0

    def _record(
        self,
        tool: str,
        source: str,
        allowed: bool,
        reason: str,
        event: MessageEvent,
    ) -> None:
        self._audit.append(
            ToolAuditEntry(
                timestamp=self._clock(),
                tool=tool,
                scope=getattr(event, "scope", ""),
                user_id=getattr(event, "user_id", ""),
                source=source,
                allowed=allowed,
                reason=reason,
            )
        )


def built_in_tools(store: MemoryRepository) -> ToolRegistry:
    registry = ToolRegistry()

    def recall(args: dict[str, Any], event: MessageEvent) -> str:
        query = str(args.get("query", ""))[:160]
        # Pass the speaker as the subject: without it this tool returns any
        # group member's *personal* memories, since they all share the scope.
        # The store filters to group-visible rows plus the speaker's own.
        return json.dumps(
            store.search_memories(
                event.scope, query, 5, subject_user_id=event.user_id
            ),
            ensure_ascii=False,
        )

    registry.register(
        Tool(
            name="recall_memory",
            description="只读：查找当前会话范围内的长期记忆。",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=recall,
            # Read-only, and scoped to the speaker by the handler above: the
            # tool the model gets out of the box holds the least authority that
            # is still useful.
            permissions=ToolPermission.READ_MEMORY,
            source="core",
        )
    )
    return registry
