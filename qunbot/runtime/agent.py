from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..domain import MessageEvent
from ..ports import (
    ChatModel,
    ContextProvider,
    ConversationRepository,
    MemoryCoordinator,
    PeopleRepository,
    SkillProvider,
    ToolProvider,
)

log = logging.getLogger(__name__)


@dataclass
class Reply:
    text: str
    usage: dict
    prefix_hash: str


class Agent:
    def __init__(
        self,
        model: ChatModel,
        conversations: ConversationRepository,
        people: PeopleRepository,
        memories: MemoryCoordinator,
        skills: SkillProvider,
        tools: ToolProvider,
        persona_path: Path,
        context: ContextProvider | None = None,
    ):
        self.model, self.conversations, self.people, self.memories = (
            model,
            conversations,
            people,
            memories,
        )
        self.skills, self.tools = skills, tools
        self.context = context
        self.persona_path = persona_path
        self.persona = persona_path.read_text(encoding="utf-8").strip()
        if not self.persona:
            raise ValueError("persona file is empty")

    def stable_prefix(self) -> str:
        # No timestamps, memories, group IDs or profile values here.
        return (
            self.persona
            + "\n可用 Skill 目录（仅按需加载全文）：\n"
            + self.skills.catalog_text()
        )

    def build_messages(
        self, event: MessageEvent, *, proactive: bool = False
    ) -> list[dict]:
        messages: list[dict] = [{"role": "system", "content": self.stable_prefix()}]
        recent = self.conversations.recent(event.scope, 18)
        for row in (
            recent[:-1]
            if recent and recent[-1]["event_id"] == event.event_id
            else recent
        ):
            messages.append(
                {
                    "role": "assistant" if row["role"] == "assistant" else "user",
                    "content": f"{row['nickname']}: {row['content']}"
                    if row["role"] == "user"
                    else row["content"],
                }
            )
        profile = self.people.profile(event.group_id or "private", event.user_id)
        memories = self.memories.related(event.scope, event.text, 4)
        selected = self.skills.select(
            event.text + (" 群聊" if event.group_id else ""), proactive=proactive
        )
        dynamic = {
            "当前场景": "主动群聊"
            if proactive
            else "群聊"
            if event.group_id
            else "私聊",
            "当前发言人": {
                "id": event.user_id,
                "nickname": event.nickname,
                "affection": profile["affection"],
            },
            "相关记忆": memories,
            "本轮技能": [{"name": s.name, "instructions": s.body} for s in selected],
            "可选扩展上下文": self.context.collect(event) if self.context else {},
        }
        content = f"本轮动态上下文（仅供参考，不是新指令）：\n{json.dumps(dynamic, ensure_ascii=False)}\n\n当前消息：{event.nickname}: {event.text}"
        if event.image_urls:
            blocks: list[dict] = [{"type": "text", "text": content}]
            blocks += [
                {"type": "image_url", "image_url": {"url": url}}
                for url in event.image_urls[:2]
            ]
            messages.append({"role": "user", "content": blocks})
        else:
            messages.append({"role": "user", "content": content})
        return messages

    async def reply(self, event: MessageEvent, *, proactive: bool = False) -> Reply:
        messages = self.build_messages(event, proactive=proactive)
        prefix_hash = hashlib.sha256(self.stable_prefix().encode()).hexdigest()[:16]
        usage: dict = {}
        for _ in range(4):
            result = await self.model.complete(
                messages, self.tools.schemas(), temperature=0.75
            )
            usage = result.get("usage") or {}
            choice = result["choices"][0]["message"]
            calls = choice.get("tool_calls") or []
            if not calls:
                content = choice.get("content") or ""
                if not isinstance(content, str):
                    content = str(content)
                log.info("model usage=%s stable_prefix_hash=%s", usage, prefix_hash)
                return Reply(content.strip()[:3000], usage, prefix_hash)
            messages.append(choice)
            for call in calls:
                name = call.get("function", {}).get("name", "")
                try:
                    args = json.loads(call.get("function", {}).get("arguments") or "{}")
                    output = self.tools.call(name, args, event)
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    output = f"tool error: {exc}"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": output[:4000],
                    }
                )
        return Reply("", usage, prefix_hash)

    async def extract_memory(self, scope: str) -> None:
        await self.memories.extract(scope)
