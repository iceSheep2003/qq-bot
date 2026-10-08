from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToneSignal:
    polarity: float = 0.0
    intensity: float = 0.0
    question: bool = False
    joking: bool = False
    hostile: bool = False


@dataclass(frozen=True)
class MessageView:
    event_id: str
    platform_message_id: str
    user_id: str
    nickname: str
    text: str
    timestamp: int
    reply_to_message_id: str = ""
    at_users: tuple[str, ...] = ()
    # QQ timestamps only have second precision; keep actual arrival order.
    sequence: int = 0
    origin: str = "human"


@dataclass(frozen=True)
class ReplyTarget:
    focus_message_id: str
    target_user_id: str
    quoted_message_id: str
    topic_id: str
    confidence: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TopicView:
    topic_id: str
    title_hint: str
    messages: tuple[MessageView, ...]
    ambiguous_topic_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationFrame:
    frame_id: str
    scope: str
    focus: MessageView
    target: ReplyTarget
    recent_window: tuple[MessageView, ...]
    topic: TopicView
    ambient: tuple[MessageView, ...]
    room_tone: ToneSignal

    def prompt_payload(self) -> dict:
        def message(item: MessageView) -> dict:
            return {
                "id": item.platform_message_id or item.event_id,
                "speaker": item.nickname,
                "user_id": item.user_id,
                "text": item.text,
                "reply_to": item.reply_to_message_id,
                "origin": item.origin,
            }

        return {
            "frame_id": self.frame_id,
            "reading_rule": (
                "recent_scene 是本轮回复的第一依据，按真实到达顺序阅读；"
                "topic_hint 只是较长期的背景索引，不得覆盖现场语境。请自行判断当前消息"
                "在接哪一句、应延续哪层意思；找不到明确对象时不要补全。"
            ),
            "local_target_hint": {
                "user_id": self.target.target_user_id,
                "quoted_message_id": self.target.quoted_message_id,
                "confidence": round(self.target.confidence, 3),
                "reasons": self.target.reasons,
            },
            "recent_scene": [message(m) for m in self.recent_window],
            "topic_hint": {
                "id": self.topic.topic_id,
                "title_hint": self.topic.title_hint,
                "ambiguous_with": self.topic.ambiguous_topic_ids,
                "authority": "background_only",
            },
            "room_tone": {
                "polarity": round(self.room_tone.polarity, 2),
                "intensity": round(self.room_tone.intensity, 2),
                "question": self.room_tone.question,
                "joking": self.room_tone.joking,
                "hostile": self.room_tone.hostile,
            },
        }
