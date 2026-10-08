"""Capability-aware policy that turns model preferences into a safe plan."""

from __future__ import annotations

from dataclasses import dataclass
import re

from ..domain import MessageEvent
from .models import ReplyDraft, ReplyPlan, VOICE_STYLES
from .media_policy import CasualMediaPolicy
from .qq_faces import QQFacePolicy
from .quotes import QuotePolicy


def _voice_short_text(value: str, limit: int = 15) -> str:
    """Keep TTS natural: only a very short, human-like utterance."""
    value = " ".join(value.split()).strip()
    if len(value) <= limit:
        return value
    for mark in ("。", "！", "？", ".", "!", "?"):
        position = value.find(mark)
        if 0 < position < limit:
            return value[: position + 1]
    # Do not truncate into a misleading sentence: the planner will fall back
    # to text when the model supplied more than the voice budget.
    return value


def _natural_close(text: str) -> str:
    """Remove routine question tails from conversational replies."""
    value = " ".join(text.split()).strip()
    if not value:
        return value
    routine_tails = (
        "你觉得呢", "你怎么看", "对吧", "是不是", "好不好", "行不行",
        "可以吗", "怎么样", "有道理吗", "懂了吗", "明白了吗",
    )
    for tail in routine_tails:
        if value.endswith(tail + "？") or value.endswith(tail + "?"):
            return value[: -len(tail) - 1].rstrip("，,。.!！ ") + "。"
    if value.endswith(("？", "?")):
        return value[:-1].rstrip() + "。"
    return value


@dataclass(frozen=True)
class ReplyCapabilities:
    meme_tags: frozenset[str] = frozenset()
    voice: bool = False


class DefaultReplyPlanner:
    def __init__(self, capabilities: ReplyCapabilities = ReplyCapabilities(), *, qq_faces: QQFacePolicy | None = None, quotes: QuotePolicy | None = None, casual_media: CasualMediaPolicy | None = None):
        self.capabilities = capabilities
        self.qq_faces = qq_faces or QQFacePolicy(probability=0.0)
        self.quotes = quotes or QuotePolicy(probability=0.0)
        self.casual_media = casual_media

    async def plan(self, draft: ReplyDraft, event: MessageEvent | None = None) -> ReplyPlan:
        requested = set(draft.channels)
        reasons: list[str] = []
        text = draft.text.strip()
        if draft.intent not in {"clarify", "ask_essential"}:
            text = _natural_close(text)
        tag = draft.meme_tag if draft.meme_tag in self.capabilities.meme_tags else ""
        voice_text = _voice_short_text((draft.voice_text or text).strip())

        if self.casual_media is not None and event is not None and event.group_id:
            # The model proposes a channel; the planner makes the final choice.
            # Apply one probability gate to both model-requested voice and
            # casual text-to-voice, otherwise their rates add together.
            wants_voice = "voice" in requested
            eligible = self.capabilities.voice and self.casual_media.voice_suitable(
                voice_text if wants_voice else text
            )
            if len(voice_text) > 15:
                eligible = False
            if eligible and self.casual_media.random_value() < self.casual_media.voice_probability:
                if requested == {"text"}:
                    requested = {"voice"}
                    reasons.append("casual voice")
            elif wants_voice:
                requested.discard("voice")
                if not requested and voice_text:
                    requested.add("text")
                    text = voice_text
                reasons.append("voice rate or suitability gate")
            if ("text" in requested and "meme" not in requested
                    and self.casual_media.random_value() < self.casual_media.followup_meme_probability):
                tag = tag or self.casual_media.followup_tag(text, self.capabilities.meme_tags)
                if tag:
                    requested.add("meme")
                    reasons.append("followup meme")
            if ("text" in requested and "meme" not in requested
                    and self.casual_media.random_value() < self.casual_media.random_meme_probability):
                tag = self.casual_media.random_tag(text, self.capabilities.meme_tags)
                if tag:
                    requested.add("meme")
                    reasons.append("random casual meme")

        if "meme" in requested and not tag:
            requested.discard("meme")
            reasons.append("meme unavailable")
        if "voice" in requested and (not self.capabilities.voice or not voice_text):
            requested.discard("voice")
            reasons.append("voice unavailable")
        # Repeating the same sentence as text and audio feels mechanical and
        # wastes a message. A combined reply is allowed only when the two parts
        # carry genuinely different content; otherwise voice replaces text.
        if "voice" in requested and "text" in requested:
            normalize = lambda value: re.sub(r"[\W_]+", "", value, flags=re.UNICODE).lower()
            left, right = normalize(text), normalize(voice_text)
            if left and right and (left == right or left in right or right in left):
                requested.discard("text")
                reasons.append("duplicate text suppressed for voice")
        if "text" in requested and not text:
            requested.discard("text")
        # Media failure must still leave a useful response whenever the model
        # supplied semantic text. This is also the deterministic fallback for
        # deployments that disable all optional media extensions.
        if not requested and text:
            requested.add("text")
            reasons.append("fallback to text")

        roster = {
            str(value) for value in (
                *((event.user_id,) if event else ()),
                *((event.at_users or ()) if event else ()),
            ) if value
        }
        at_user = draft.at_user_id if draft.at_user_id in roster else ""
        qq_face = self.qq_faces.choose(text)
        quote_message_id = self.quotes.choose(draft, event)
        ordered = tuple(channel for channel in ("text", "meme", "voice") if channel in requested)
        return ReplyPlan(
            text=text,
            channels=ordered,
            meme_tag=tag,
            voice_text=voice_text,
            at_user_id=at_user,
            qq_face_id=qq_face[1] if qq_face else "",
            qq_face_name=qq_face[0] if qq_face else "",
            quote_message_id=quote_message_id,
            reason=", ".join(reasons) or "model preference accepted",
            voice_style=draft.voice_style if draft.voice_style in VOICE_STYLES else "neutral",
        )
