"""Reply-media policy. Marker syntax is private to the bot; no chat commands."""

from __future__ import annotations

import logging
import re
from typing import Protocol

log = logging.getLogger(__name__)

MEME_MARKER = re.compile(r"\[\[meme:([a-zA-Z0-9_-]{1,40})\]\]")
VOICE_MARKER = "[[voice]]"


def strip_markers(text: str) -> str:
    """Remove bot-private media markers without producing media."""
    return MEME_MARKER.sub("", text).replace(VOICE_MARKER, "").strip()


class MemeSource(Protocol):
    def pick(self, tag: str) -> str | None: ...


class SpeechSource(Protocol):
    async def synthesize(self, text: str) -> str: ...


class ReplyMediaProcessor:
    def __init__(self, memes: MemeSource, speech: SpeechSource | None = None):
        self.memes, self.speech = memes, speech

    async def compose(self, text: str) -> tuple[str, str | None, str | None]:
        match = MEME_MARKER.search(text)
        tag = match.group(1) if match else None
        clean = MEME_MARKER.sub("", text).replace(VOICE_MARKER, "").strip()
        image = self.memes.pick(tag) if tag else None
        voice = None
        if VOICE_MARKER in text and self.speech and clean:
            try:
                voice = await self.speech.synthesize(clean[:300])
            except Exception:
                log.exception("Speech synthesis failed; sending text fallback")
        return clean, image, voice
