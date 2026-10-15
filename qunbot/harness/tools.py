"""The ONLY model-facing tool router: 14 versioned schemas, scoped receipts."""

from __future__ import annotations
import inspect
import json
import time
from collections import defaultdict, deque

from ..contracts.kernel_v1 import (
    model_tool_schemas,
    tool_names,
    validate_tool_input,
    validate_tool_output,
)
from ..contracts.schema import ContractError, canonical, fingerprint
from ..ontology.objects import stable_id

GROUP_ONLY_TOOLS = frozenset({"group_query", "group_action_propose", "interaction_propose"})


class KernelDenied(ContractError):
    pass


class KernelTools:
    def __init__(
        self, handlers: dict, *, allowed_scopes: frozenset[str], executions=None,
        private_enabled: bool = False,
    ):
        if set(handlers) - set(tool_names()):
            raise ContractError("Unknown kernel handler")
        self._handlers = dict(handlers)
        self._scopes = frozenset(allowed_scopes)
        self.private_enabled = private_enabled
        self.executions = executions
        self._calls = defaultdict(deque)

    def schemas(self):
        return model_tool_schemas()

    def schemas_for(self, event):
        if event.scope.startswith("private:"):
            return [s for s in self.schemas() if s["function"]["name"] not in GROUP_ONLY_TOOLS]
        return self.schemas()

    def _scope_allowed(self, event):
        if event.scope.startswith("private:"):
            return (
                self.private_enabled and not getattr(event, "group_id", None)
                and bool(getattr(event, "user_id", ""))
                and event.scope == f"private:{event.user_id}"
            )
        return event.scope in self._scopes

    async def acall(self, name, args, event):
        trace = stable_id("tool", event.scope, event.event_id, name, fingerprint(args))

        def error(status, code, message):
            return {
                "status": status,
                "trace_id": trace,
                "data": None,
                "error": {"code": code, "message": message},
            }

        previous = None
        try:
            validate_tool_input(name, args)
            if not self._scope_allowed(event):
                raise KernelDenied("Conversation scope is not enabled")
            if event.scope.startswith("private:") and name in GROUP_ONLY_TOOLS:
                raise KernelDenied("Group capabilities are unavailable in private chat")
            key = (event.scope, name)
            calls = self._calls[key]
            now = time.monotonic()
            while calls and calls[0] < now - 60:
                calls.popleft()
            if len(calls) >= 20:
                raise KernelDenied("Kernel tool quota reached")
            calls.append(now)
            handler = self._handlers.get(name)
            if handler is None:
                result = error(
                    "unavailable",
                    "unavailable",
                    "This built-in capability has no configured provider",
                )
            else:
                if (
                    name in {"interaction_propose", "group_action_propose"}
                    and self.executions
                ):
                    previous = self.executions.begin(
                        event.scope, trace, name, fingerprint(args)
                    )
                    if previous is not None:
                        result = (
                            json.loads(previous["result"])
                            if previous.get("result")
                            else error(
                                "unknown",
                                "unknown",
                                "An earlier execution has no confirmed outcome; not retried",
                            )
                        )
                        return canonical(result).decode()
                data = handler(args, event)
                if inspect.isawaitable(data):
                    data = await data
                result = {
                    "status": "ok",
                    "trace_id": trace,
                    "data": data,
                    "error": None,
                }
        except KernelDenied:
            result = error("denied", "denied", "Host policy refused this operation")
        except ContractError:
            result = error(
                "invalid_input",
                "invalid_input",
                "Arguments or result violate the approved contract",
            )
        except TimeoutError:
            result = error(
                "unknown",
                "unknown",
                "External outcome is uncertain; automatic retry refused",
            )
        except (ConnectionError, NotImplementedError):
            result = error("unavailable", "unavailable", "Provider unavailable")
        except Exception:
            # Don't expose exception text: it may contain external responses,
            # credentials, endpoint query strings or private values.
            result = error("failed", "upstream_failure", "Provider operation failed")
        try:
            validate_tool_output(name, result)
        except ContractError:
            result = error(
                "failed",
                "upstream_failure",
                "Provider returned an invalid bounded result",
            )
        if (
            name in {"interaction_propose", "group_action_propose"}
            and self.executions
            and previous is None
        ):
            # Only settle if begin actually happened (validation can fail first).
            if self.executions.pending(event.scope, trace):
                self.executions.settle(
                    event.scope,
                    trace,
                    "succeeded"
                    if result["status"] == "ok"
                    else "unknown"
                    if result["status"] == "unknown"
                    else "failed",
                    result,
                )
        return canonical(result).decode()
