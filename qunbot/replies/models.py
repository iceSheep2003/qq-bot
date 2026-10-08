"""Framework-neutral values passed through the reply pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ReplyChannel = Literal["text", "meme", "voice"]
VoiceStyle = Literal["neutral", "warm", "playful", "excited", "serious"]
VOICE_STYLES = frozenset(("neutral", "warm", "playful", "excited", "serious"))


@dataclass(frozen=True)
class ReplyDraft:
    """What the model wants to say and how it would prefer to express it."""

    text: str
    channels: tuple[ReplyChannel, ...] = ("text",)
    meme_tag: str = ""
    voice_text: str = ""
    at_user_id: str = ""
    intent: str = "reply"
    state_observations: dict[str, Any] | None = None
    topic_update: dict[str, Any] | None = None
    conversation_decision: dict[str, Any] | None = None
    voice_style: VoiceStyle = "neutral"


@dataclass(frozen=True)
class ReplyPlan:
    """A capability-checked delivery decision, safe to hand to a dispatcher."""

    text: str
    channels: tuple[ReplyChannel, ...]
    meme_tag: str = ""
    voice_text: str = ""
    at_user_id: str = ""
    qq_face_id: str = ""
    qq_face_name: str = ""
    quote_message_id: str = ""
    reason: str = ""
    voice_style: VoiceStyle = "neutral"

    @property
    def semantic_text(self) -> str:
        return self.text or self.voice_text


@dataclass(frozen=True)
class DeliveryResult:
    """What was actually delivered, including media-only replies."""

    transcript: str
    sent_text: bool = False
    sent_meme: bool = False
    sent_voice: bool = False
    sent_qq_face: bool = False
    at_user_id: str = ""
    quote_message_id: str = ""

    @property
    def sent(self) -> bool:
        return self.sent_text or self.sent_meme or self.sent_voice or self.sent_qq_face
