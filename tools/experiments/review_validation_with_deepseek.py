"""Blindly review current-model validation drafts with DeepSeek."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from qqbot.infrastructure.adapters.model import ModelClient
from qqbot.infrastructure.adapters.structured import InstructorOutput


STRATEGIES = Literal[
    "answer_directly", "acknowledge", "agree", "empathize", "banter",
    "tease_gently", "supplement", "clarify", "encourage", "deescalate",
    "topic_shift", "celebrate", "abstain",
]
ACTIONS = Literal[
    "answer", "acknowledge", "agree", "empathize", "joke", "tease",
    "add_information", "clarify", "encourage", "calm", "bridge_topic",
    "celebrate", "wait",
]
MEDIA = Literal["none", "meme", "voice", "text_plus_meme", "text_plus_voice", "native_reaction"]
FAMILIES = Literal[
    "direct_question", "banter_or_joke", "emotional_complaint",
    "agreement_or_consensus", "clarification_or_dispute", "progress_or_good_news",
    "topic_shift_or_cold_room", "privacy_or_uncertainty", "general_group_exchange",
]


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1, max_length=20)
    verdict: Literal["accept", "correct", "reject"]
    situation_family: FAMILIES
    response_mode: Literal["respond", "wait"]
    strategy: STRATEGIES
    dialogue_act: ACTIONS
    media_intent: MEDIA
    avoid: list[str] = Field(max_length=6)
    confidence: float = Field(ge=0, le=1)
    issues: list[str] = Field(max_length=5)
    rationale: str = Field(min_length=1, max_length=300)


class ReviewBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviews: list[Review] = Field(max_length=8)


PROMPT = """你是群聊 Agent 行为标注复核器。你要复核当前模型给出的 draft，而不是评价历史群友本人。
每个 packet 的 current_message_id 标记了机器人将要面对的当前入站消息；context 中没有未来消息。
请判断机器人此刻更适合回应还是等待，并选择一个策略、对话动作和媒体意图。

规则：
1. 只输出 ReviewBatch JSON，sample_id 必须来自输入。
2. 如果 draft 合理，verdict=accept；需要改策略/动作/场景/媒体时 verdict=correct；上下文不足、内容不可判断或不适合用于训练时 verdict=reject。
3. 不要把历史群友的实际回复当成 bot 必须复现的台词，只抽象参与方式。
4. 不要把明显玩笑按字面执行；不要推荐连续提问、长篇说教或未经证实的事实。
5. media_intent 只是表达倾向，不代表真的有图片或语音；看不到媒体内容时宁可 none。
6. confidence 代表你对这条标注的判断，不是模型能力或事实真值。
7. 只给简短 rationale，不复述原始群友身份、QQ号、昵称、链接或敏感信息。
"""


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def render(packet: dict) -> str:
    current = packet["current_message_id"]
    lines = [f"PACKET {packet['sample_id']} current={current}"]
    for msg in packet["context"]:
        marker = " [CURRENT]" if msg["is_current"] else ""
        reply = f" reply_to={msg['reply_to']}" if msg["reply_to"] else ""
        media = " [MEDIA_UNKNOWN]" if msg["unknown_media"] else ""
        lines.append(f"{msg['id']} {msg['speaker']}{marker}{reply}{media}: {msg['text']}")
    lines.append("CURRENT-MODEL-DRAFT: " + json.dumps(packet["assistant_draft"], ensure_ascii=False))
    return "\n".join(lines)


async def run(args):
    packets = [json.loads(line) for line in args.packets.open(encoding="utf-8") if line.strip()]
    cfg = read_env(args.env)
    required = {"BOT_MODEL_BASE_URL", "BOT_MODEL_API_KEY", "BOT_MODEL_NAME"}
    if required - cfg.keys():
        raise SystemExit("model env is incomplete")
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoint_path = output.with_suffix(".checkpoint.json")
    checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.exists() else {"next": 0, "reviews": 0, "errors": 0}
    if not 0 <= checkpoint["next"] <= len(packets):
        raise SystemExit("invalid checkpoint")
    model = ModelClient(
        cfg["BOT_MODEL_BASE_URL"], cfg["BOT_MODEL_API_KEY"], cfg["BOT_MODEL_NAME"],
        timeout=90, max_completion_tokens=1800, thinking_enabled=False,
    )
    structured = InstructorOutput(model, max_attempts=2)
    try:
        for offset in range(checkpoint["next"], len(packets), args.batch_size):
            batch = packets[offset : offset + args.batch_size]
            content = "\n\n".join(render(packet) for packet in batch)
            messages = [
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": "请复核以下脱敏 packet：\n\n" + content},
            ]
            try:
                data, usage = await structured.propose(messages, ReviewBatch)
                reviews = [item for item in data["reviews"] if item["sample_id"] in {p["sample_id"] for p in batch}]
                with output.open("a", encoding="utf-8") as stream:
                    for review in reviews:
                        stream.write(json.dumps({"review": review, "usage": usage}, ensure_ascii=False) + "\n")
                # The reviewed packet is still private derived data.  Keep
                # the append-created file restricted even when the output
                # directory already exists.
                os.chmod(output, 0o600)
                checkpoint.update(next=offset + len(batch), reviews=checkpoint["reviews"] + len(reviews), last_usage=usage)
            except Exception as exc:  # bounded batch failure; no raw model output logged
                checkpoint.update(next=offset + len(batch), errors=checkpoint["errors"] + 1, last_error=type(exc).__name__)
                errors_path = output.with_suffix(".errors.jsonl")
                with errors_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"offset": offset, "error": type(exc).__name__}, ensure_ascii=False) + "\n")
                os.chmod(errors_path, 0o600)
            tmp = checkpoint_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(checkpoint_path)
    finally:
        await model.client.aclose()
    print(json.dumps(checkpoint, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env", type=Path, default=Path("data/private/deployment/deepseek-20261014.env"))
    parser.add_argument("--batch-size", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
