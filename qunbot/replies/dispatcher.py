"""Resolve a reply plan and send it without exposing platform commands to AI."""

from __future__ import annotations

from ..ports import MediaProcessor, MessageSender
from .models import DeliveryResult, ReplyPlan
from .pacing import HumanizedPacer
from .segmentation import TextSegmenter


class ReplyDispatcher:
    def __init__(self, sender: MessageSender, media: MediaProcessor | None = None, pacer: HumanizedPacer | None = None, segmenter: TextSegmenter | None = None):
        self.sender, self.media = sender, media
        self.pacer = pacer
        self.segmenter = segmenter or TextSegmenter.from_env()

    async def _send_text_parts(self, text: str, *, group_id, user_id, first: dict | None = None) -> None:
        parts = self.segmenter.split(text)
        for index, part in enumerate(parts):
            if index and self.pacer is not None:
                await self.pacer.between_parts()
            kwargs = dict(first or {}) if index == 0 else {}
            kwargs["text"] = part
            await self.sender.send(group_id=group_id, user_id=user_id, **kwargs)

    async def dispatch(
        self, plan: ReplyPlan, *, group_id: str | None, user_id: str | None,
        allowed_at: frozenset[str] = frozenset(),
    ) -> DeliveryResult:
        if not plan.channels:
            return DeliveryResult("")
        if self.pacer is not None:
            await self.pacer.before(plan)
        # MediaProcessor owns concrete image/TTS resolution. The marker string
        # is now an internal compatibility wire format, never model-facing.
        visible_text = plan.text if "text" in plan.channels else ""
        synthesis_text = plan.voice_text or plan.text
        wire_text = visible_text
        if "meme" in plan.channels and plan.meme_tag:
            wire_text += f" [[meme:{plan.meme_tag}]]"
        if plan.at_user_id:
            wire_text += f" [[at:{plan.at_user_id}]]"

        if self.media is None:
            if plan.text:
                await self._send_text_parts(
                    plan.text, group_id=group_id, user_id=user_id,
                    first={"at_user": plan.at_user_id or None,
                           "qq_face": plan.qq_face_id or None,
                           "reply_to": plan.quote_message_id or None},
                )
                return DeliveryResult(plan.text, sent_text=True, sent_qq_face=bool(plan.qq_face_id), at_user_id=plan.at_user_id)
            return DeliveryResult("")

        # Resolve the visible part and spoken part independently. Marker-based
        # composition cannot otherwise represent text=A and voice=B without
        # accidentally synthesising A twice.
        message = await self.media.compose_message(wire_text, allowed_at=allowed_at)
        kwargs = message.send_kwargs()
        if plan.qq_face_id:
            kwargs["qq_face"] = plan.qq_face_id
        if plan.quote_message_id:
            kwargs["reply_to"] = plan.quote_message_id
        voice = None
        if "voice" in plan.channels and synthesis_text:
            spoken = await self.media.compose_message(
                f"{synthesis_text} [[voice]]", voice_style=plan.voice_style,
            )
            voice = spoken.voice
        if "text" not in plan.channels:
            kwargs.pop("text", None)
        sent_text = bool(kwargs.get("text"))
        sent_meme = bool(kwargs.get("image"))
        sent_qq_face = bool(kwargs.get("qq_face"))
        if sent_text:
            text = str(kwargs.pop("text"))
            image = kwargs.pop("image", None)
            await self._send_text_parts(text, group_id=group_id, user_id=user_id, first=kwargs)
            if image:
                if self.pacer is not None:
                    await self.pacer.between_parts()
                await self.sender.send(group_id=group_id, user_id=user_id, image=image)
        elif sent_meme or sent_qq_face:
            await self.sender.send(group_id=group_id, user_id=user_id, **kwargs)
        sent_voice = bool(voice)
        if voice:
            if (sent_text or sent_meme or sent_qq_face) and self.pacer is not None:
                await self.pacer.between_parts()
            await self.sender.send(group_id=group_id, user_id=user_id, voice=voice)
        # A catalogue entry may disappear, validation may reject a file, or
        # TTS may time out after planning.  Those are delivery failures, not a
        # reason to lose the bot's semantic reply.  Fall back here, where the
        # dispatcher knows what actually resolved (the planner only knows what
        # was configured).
        if not (sent_text or sent_meme or sent_voice) and plan.text:
            await self._send_text_parts(
                plan.text, group_id=group_id, user_id=user_id,
                first={"at_user": plan.at_user_id or None,
                       "reply_to": plan.quote_message_id or None},
            )
            sent_text = True
        labels = []
        if sent_meme:
            labels.append(f"[表情包:{plan.meme_tag}]")
        if sent_voice and not plan.text:
            labels.append("[语音]")
        transcript = plan.semantic_text or " ".join(labels)
        return DeliveryResult(
            transcript=transcript,
            sent_text=sent_text,
            sent_meme=sent_meme,
            sent_voice=sent_voice,
            sent_qq_face=sent_qq_face,
            at_user_id=plan.at_user_id,
            quote_message_id=plan.quote_message_id,
        )
