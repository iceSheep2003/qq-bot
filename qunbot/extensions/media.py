"""Reply-media policy. Marker syntax is private to the bot; no chat commands.

Parsing lives in :mod:`qunbot.extensions.media_parts`: the reply text is turned
into explicit ``Text`` / ``Image`` / ``Voice`` / ``At`` parts by one bounded,
end-anchored scan. This module resolves those parts against the meme catalog
and the (optional) speech provider.

Failure policy for every media backend call: **degrade, never fail the turn**.
A catalog miss, a TTS exception and an oversized or malformed source all drop
that one part and leave the text intact. Nothing here constructs an HTTP client
of its own, so disabling voice means no TTS client is created at all.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Protocol

from .media_parts import (
    Image,
    OutboundMessage,
    Voice,
    max_image_bytes,
    parse_outbound,
    strip_markers,
    valid_image_source,
    valid_voice_source,
)

__all__ = [
    "ReplyMediaProcessor",
    "MemeSource",
    "SpeechSource",
    "strip_markers",
]

log = logging.getLogger(__name__)


class MemeSource(Protocol):
    def pick(self, tag: str) -> str | None: ...


class SpeechSource(Protocol):
    async def synthesize(self, text: str) -> str: ...


class ReplyMediaProcessor:
    def __init__(
        self,
        memes: MemeSource,
        speech: SpeechSource | None = None,
        *,
        image_bytes_limit: int | None = None,
    ):
        self.memes, self.speech = memes, speech
        self.image_bytes_limit = (
            image_bytes_limit if image_bytes_limit is not None else max_image_bytes()
        )

    def parse(
        self, text: str, *, allowed_at: Collection[str] | None = None
    ) -> OutboundMessage:
        """Syntax only: no catalog lookup, no TTS call, no I/O."""
        return parse_outbound(text, allowed_at=allowed_at)

    async def compose_message(
        self, text: str, *, allowed_at: Collection[str] | None = None,
        voice_style: str = "neutral",
    ) -> OutboundMessage:
        """Parse, then resolve each media part. Never raises for media faults."""
        return await self.resolve(self.parse(text, allowed_at=allowed_at), voice_style=voice_style)

    async def resolve(self, message: OutboundMessage, *, voice_style: str = "neutral") -> OutboundMessage:
        resolved = []
        for part in message.parts:
            if isinstance(part, Image):
                source = part.source or self._pick(part.ref or part.tag)
                if source and not valid_image_source(
                    source, max_bytes=self.image_bytes_limit
                ):
                    log.warning("Dropping oversized or invalid image source")
                    source = ""
                if source:
                    resolved.append(
                        Image(source=source, tag=part.tag or part.ref, ref=part.ref)
                    )
            elif isinstance(part, Voice):
                source = part.source or await self._synthesize(part.text, voice_style=voice_style)
                if source and not valid_voice_source(source):
                    log.warning("Dropping invalid voice source")
                    source = ""
                if source:
                    resolved.append(Voice(source=source, ref=part.ref, text=part.text))
            else:
                resolved.append(part)
        return OutboundMessage(tuple(resolved))

    def _pick(self, tag: str) -> str:
        if not tag:
            return ""
        try:
            return self.memes.pick(tag) or ""
        except Exception:
            log.exception("Meme lookup failed for tag %r", tag)
            return ""

    async def _synthesize(self, text: str, *, voice_style: str = "neutral") -> str:
        # No provider configured (voice disabled) means no client exists and no
        # request is attempted; the reply is simply text.
        if self.speech is None or not text:
            return ""
        try:
            styled = getattr(self.speech, "synthesize_styled", None)
            if callable(styled):
                return await styled(text[:300], voice_style) or ""
            return await self.speech.synthesize(text[:300]) or ""
        except Exception:
            log.exception("Speech synthesis failed; sending text fallback")
            return ""

    async def compose(self, text: str) -> tuple[str, str | None, str | None]:
        """Legacy three-tuple view. Kept because ``ports.MediaProcessor`` is frozen."""
        message = await self.compose_message(text)
        return message.text, message.image, message.voice
