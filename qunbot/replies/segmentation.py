"""Split one complete model reply into QQ sized messages at natural pauses."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TextSegmenter:
    max_chars: int = 180

    @classmethod
    def from_env(cls) -> "TextSegmenter":
        try:
            value = int(os.getenv("BOT_REPLY_SEGMENT_MAX_CHARS", "180"))
        except ValueError:
            value = 180
        return cls(max(60, min(1000, value)))

    def split(self, text: str) -> tuple[str, ...]:
        source = str(text or "").strip()
        if not source:
            return ()
        parts: list[str] = []
        remaining = source
        while len(remaining) > self.max_chars:
            window = remaining[: self.max_chars + 1]
            # Paragraphs and sentences are better message boundaries than a
            # comma. Only fall back to a clause or a hard limit when the model
            # wrote a single very long sentence. The model never chooses how
            # many QQ messages to send.
            cut = 0
            for pattern in (r"\n\s*\n", r"[。！？!?；;]\s*", r"[，,、：:]\s*"):
                boundaries = [m.end() for m in re.finditer(pattern, window)]
                cut = max((point for point in boundaries if point >= self.max_chars // 2), default=0)
                if cut:
                    break
            if not cut:
                cut = self.max_chars
            parts.append(remaining[:cut].strip())
            remaining = remaining[cut:].strip()
        if remaining:
            parts.append(remaining)
        return tuple(part for part in parts if part)
