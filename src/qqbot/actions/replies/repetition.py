"""Detect model echoes before sending; the explicit +1 path lives elsewhere."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


def _normalized(text: str) -> str:
    return re.sub(r"[^\w]+", "", str(text or "").casefold())


def repeats_recent(text: str, rows: list[dict]) -> bool:
    """Catch verbatim copies and near copies with only a filler added.

    Short replies need an exact match: fuzzy matching those would incorrectly
    suppress ordinary answers. Longer replies may differ by a laugh or particle
    while still being a plain echo of a member or of the bot's own last line.
    """
    candidate = _normalized(text)
    if not candidate:
        return False
    for row in rows:
        if row.get("role") not in {"user", "assistant"}:
            continue
        previous = _normalized(str(row.get("content") or ""))
        if not previous:
            continue
        if candidate == previous:
            return True
        if (
            min(len(candidate), len(previous)) >= 8
            and max(len(candidate), len(previous)) <= min(len(candidate), len(previous)) * 1.4
            and SequenceMatcher(None, candidate, previous, autojunk=False).ratio() >= 0.86
        ):
            return True
    return False
