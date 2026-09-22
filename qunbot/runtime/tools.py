from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..domain import MessageEvent
from ..ports import MemoryRepository


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], MessageEvent], str]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Model-visible capabilities registered outside the agent loop."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def call(self, name: str, args: dict[str, Any], event: MessageEvent) -> str:
        tool = self._tools.get(name)
        if not tool:
            return "unknown tool"
        return tool.handler(args, event)


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
        )
    )
    return registry
