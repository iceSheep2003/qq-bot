"""Local quote policy: the model proposes semantics, never platform IDs."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Callable

from ..domain import MessageEvent
from .models import ReplyDraft


def _probability() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("BOT_QUOTE_PROBABILITY", "0.78"))))
    except ValueError:
        return 0.78


@dataclass(frozen=True)
class QuotePolicy:
    probability: float = 0.0
    random_value: Callable[[], float] = random.random

    @classmethod
    def from_env(cls) -> "QuotePolicy":
        enabled = os.getenv("BOT_QUOTE_ENABLED", "true").lower() == "true"
        return cls(_probability() if enabled else 0.0)

    def choose(self, draft: ReplyDraft, event: MessageEvent | None) -> str:
        if event is None or not event.group_id or self.probability <= 0:
            return ""
        decision = draft.conversation_decision or {}
        proposed = str(decision.get("target_message_id") or "").strip()
        relation = str(decision.get("relation") or "").strip()
        allowed = {
            value for value in (
                event.platform_message_id,
                event.reply_to_message_id,
                *event.recent_message_ids,
            )
            if value and value.lstrip("-").isdigit()
        }
        target = proposed if proposed in allowed else ""
        if not target and event.reply_to_message_id in allowed:
            target = event.reply_to_message_id
        if not target and event.platform_message_id in allowed:
            target = event.platform_message_id
        if not target:
            return ""
        warranted = bool(event.reply_to_message_id or event.at_bot) or relation in {
            "answer", "clarify"
        }
        return target if warranted and self.random_value() < self.probability else ""
