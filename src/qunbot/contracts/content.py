"""Shared rules for transcript-only message content.

Media placeholders are useful when rendering conversation history for a model,
but they are not user-facing text and must never cross an outbound boundary.
"""

from __future__ import annotations

import re


_MEDIA_PLACEHOLDER = re.compile(
    r"^\[(?:图片|语音|表情|文件|视频|骰子|猜拳)(?:[^\]]*)?\]$"
)


def is_media_placeholder(text: object) -> bool:
    """Return whether *text* is an internal, media-only transcript marker."""
    return bool(_MEDIA_PLACEHOLDER.fullmatch(str(text or "").strip()))
