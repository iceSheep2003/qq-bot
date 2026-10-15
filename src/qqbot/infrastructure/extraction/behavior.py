"""Evidence-grounded behavior extraction from deidentified chat fragments."""

from __future__ import annotations

from qqbot.infrastructure.adapters.structured import InstructorOutput
from qqbot.agent.cognition.experience.models import BehaviorBatch


SYSTEM_PROMPT = """你是群聊行为经验抽取器，不是聊天机器人，也不是人物档案抽取器。
你的任务是从一批已经脱敏的群聊互动片段中，抽象出机器人以后如何参与群聊的可复用经验。

只输出符合 BehaviorBatch schema 的 JSON。每个输入 case 最多输出一条 proposal；没有可靠行为经验就不要输出。
case_id 必须来自输入。source_message_ids 必须来自该 case，且只能引用真实提供的消息 ID。

只抽象以下内容：场景、可观察线索、参与策略、对话动作、表达结构、应避免的做法、媒体表达倾向、话题词和互动结果。
不要记录任何人的姓名、QQ号、昵称、身份、长期性格或关系。不要把片段中的人物变成跨片段实体。
不要照抄完整聊天，不要生成固定台词，不要把“出现某个关键词”直接等同于情绪。
不要发明图片、语音、工具或管理权限。media_intent 只能描述表达倾向，不代表已经发送。
把玩笑、疑问、反讽与事实分开；无法判断时使用 uncertain 或不输出。
expression_pattern 必须是可迁移的表达结构，而不是一条必须复读的原句。
abstraction 用一句话说明这个经验对群聊 Agent 的帮助。
"""


class BehaviorExtractor:
    VERSION = "behavior-extractor-v1"

    def __init__(self, structured: InstructorOutput):
        self.structured = structured

    async def extract(self, cases: list[dict]) -> tuple[BehaviorBatch, dict]:
        if not cases:
            return BehaviorBatch(proposals=[]), {}
        payload = "\n\n".join(self._render_case(case) for case in cases)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "请抽取下面片段中的群聊行为经验。只保留可迁移的行为，不要复述原文。\n\n"
                    + payload
                ),
            },
        ]
        data, usage = await self.structured.propose(messages, BehaviorBatch)
        result = BehaviorBatch.model_validate(data)
        allowed = {case["id"] for case in cases}
        valid = []
        message_ids = {
            case["id"]: set(case["message_ids"]) for case in cases
        }
        for proposal in result.proposals:
            if proposal.case_id not in allowed:
                continue
            if not set(proposal.source_message_ids) <= message_ids[proposal.case_id]:
                continue
            valid.append(proposal)
        return BehaviorBatch(proposals=valid), usage

    @staticmethod
    def _render_case(case: dict) -> str:
        lines = [f"CASE {case['id']}"]
        for row in case["messages"]:
            marker = " [回复目标]" if row.get("is_reply_target") else ""
            lines.append(f"{row['id']} {row['speaker']}{marker}: {row['text']}")
        return "\n".join(lines)
