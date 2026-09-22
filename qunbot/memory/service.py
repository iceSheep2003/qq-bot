"""Collect, distill and retrieve scoped memories independently of the Agent loop."""

from __future__ import annotations

import json
import re

from ..ports import ChatModel, ConversationRepository, MemoryRepository


class MemoryService:
    def __init__(
        self, model: ChatModel, conversations: ConversationRepository,
        memories: MemoryRepository,
    ):
        self.model = model
        self.conversations = conversations
        self.memories = memories

    def related(self, scope: str, query: str, limit: int = 4) -> list[str]:
        return [item["content"] for item in self.memories.search_memories(scope, query, limit)]

    async def extract(self, scope: str) -> None:
        rows = self.conversations.recent(scope, 20)
        if not rows:
            return
        transcript = "\n".join(
            f"{row['nickname']}({row['user_id']}): {row['content'][:300]}"
            for row in rows
        )
        result = await self.model.complete(
            [
                {
                    "role": "system",
                    "content": "从聊天中提取最多3条明确、可长期保留的事实或偏好。不要推断私人敏感信息，不要记临时情绪。仅输出 JSON 数组，每项含 user_id 和 fact。若无则输出 []。",
                },
                {"role": "user", "content": transcript},
            ],
            temperature=0,
        )
        content = result["choices"][0]["message"].get("content") or "[]"
        match = re.search(r"\[[\s\S]*\]", content)
        if not match:
            return
        try:
            facts = json.loads(match.group())
        except json.JSONDecodeError:
            return
        known_users = {row["user_id"] for row in rows if row["role"] == "user"}
        for item in facts[:3] if isinstance(facts, list) else []:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id", ""))
            fact = str(item.get("fact", "")).strip()
            if (
                user_id in known_users
                and 5 <= len(fact) <= 300
                and not self.memories.has_memory(scope, user_id, fact)
            ):
                self.memories.remember(scope, user_id, fact)
