"""Quota-, relationship- and pressure-aware admission before model calls."""

from __future__ import annotations

import math
import random
import re
from difflib import SequenceMatcher

from ...domain import MessageEvent
from .config import ReplyPolicyConfig
from .policy import Decision, RoomReadingPolicy


class AdaptiveReplyPolicy:
    """Decorate room-reading with a decaying API-spend policy.

    Pressure is derived from persisted conversation history, so it survives a
    restart without owning another state table.  Each message from the same
    user contributes ``0.5 ** (age / half_life)``; old pressure therefore
    disappears smoothly instead of resetting at an arbitrary window edge.
    """

    name = "adaptive_room"

    def __init__(self, config: ReplyPolicyConfig, *, random_value=None):
        self.config = config
        self.room = RoomReadingPolicy(config)
        self.random_value = random_value or random.random
        self.service = None
        self.last: Decision | None = None
        self._admitted: set[str] = set()

    def bind(self, service) -> None:
        self.service = service

    async def decide(self, event: MessageEvent, *, recent: list[dict]) -> bool:
        direct = self._direct(event)
        room = self.room.evaluate(event, recent)
        if not direct and not room.reply:
            self.last = room
            return False
        if self.service is None:
            # Pure/replay use remains deterministic and has no infrastructure.
            self.last = room
            return bool(room.reply)

        group_id = event.group_id or "private"
        today = self.service.today_start()
        source = f"interject:{event.user_id}"
        activity = self.service.activity
        group_used = activity.proactive_count_prefix_since(
            group_id, today, "interject:"
        )
        user_used = activity.proactive_count_since(group_id, today, source)
        if group_used >= self.config.daily_group_limit:
            return self._deny("今日接话总额度已用完")
        if user_used >= self.config.daily_user_limit:
            return self._deny("该用户今日接话额度已用完")

        pressure, repeated = self._pressure(event, recent)
        affection = int(
            self.service.people.profile(group_id, event.user_id).get("affection", 0)
        )
        relationship = max(0.45, min(1.55, 1.0 + affection / 100.0 * 0.55))
        continued = self._continued_thread(event, recent, room)
        base = self.config.mention_probability if direct else (
            self.config.after_reply_probability
            if continued
            else self.config.ambient_probability
        )
        probability = base * relationship * math.exp(
            -self.config.decay_strength * pressure
        )
        is_question = any(
            signal.startswith("有人在提问") for signal in room.signals
        )
        if is_question and not repeated:
            probability = max(
                probability, self.config.question_probability_floor * relationship
            )
        if repeated:
            probability *= self.config.repeat_multiplier
        probability = max(0.0, min(0.98, probability))
        if self.random_value() > probability:
            return self._deny(
                f"衰退后不回复（p={probability:.3f}, pressure={pressure:.2f}, "
                f"affection={affection}, repeated={repeated}）"
            )
        self._admitted.add(event.event_id)
        self.last = Decision(
            True,
            f"允许回复（p={probability:.3f}, pressure={pressure:.2f}, "
            f"affection={affection}, direct={direct}, continued={continued}）",
            probability,
            ("direct" if direct else "continued" if continued else "ambient",),
        )
        return True

    def record_delivery(self, event: MessageEvent, text: str) -> None:
        if self.service is None or event.event_id not in self._admitted:
            return
        self._admitted.discard(event.event_id)
        self.service.activity.log_proactive(
            event.group_id or "private",
            f"{event.user_id}:{text[:120]}",
            f"interject:{event.user_id}",
        )

    def _direct(self, event: MessageEvent) -> bool:
        if not event.group_id or event.at_bot:
            return True
        return any(name and name in (event.text or "") for name in self.config.bot_names)

    def _pressure(self, event: MessageEvent, recent: list[dict]) -> tuple[float, bool]:
        now = int(event.timestamp or 0)
        current = self._normal(event.text)
        pressure = 0.0
        repeated = False
        compared = 0
        # Only unanswered attempts create pressure. Messages before the most
        # recent assistant turn were already answered; counting them made a
        # healthy back-and-forth less likely after every successful reply.
        unanswered: list[dict] = []
        for row in reversed(recent or []):
            if row.get("role") == "assistant":
                break
            unanswered.append(row)
        for row in unanswered:
            if row.get("event_id") == event.event_id or row.get("role") == "assistant":
                continue
            if str(row.get("user_id") or "") != str(event.user_id):
                continue
            age = max(0, now - int(row.get("created_at") or 0))
            # A pursuit is one short conversational burst, not a permanent
            # property of the user. The half-life smooths pressure *inside*
            # this window; beyond it the old turn contributes exactly zero.
            if age > self.config.pursuit_window_seconds:
                continue
            pressure += 0.5 ** (age / self.config.decay_half_life_seconds)
            if current and compared < 5:
                previous = self._normal(str(row.get("content") or ""))
                repeated = repeated or (
                    current == previous
                    or SequenceMatcher(None, current, previous).ratio() >= 0.88
                )
                compared += 1
        return pressure, repeated

    def _continued_thread(
        self, event: MessageEvent, recent: list[dict], room: Decision
    ) -> bool:
        if not room.reply or self.config.after_reply_duration_seconds <= 0:
            return False
        now = int(event.timestamp or 0)
        for row in reversed(recent or []):
            if row.get("role") != "assistant":
                continue
            try:
                age = now - int(row.get("created_at") or 0)
            except (TypeError, ValueError):
                return False
            return 0 <= age <= self.config.after_reply_duration_seconds
        return False

    @staticmethod
    def _normal(text: str) -> str:
        return re.sub(r"\s+|[@＠][^\s]+", "", text or "").lower()

    def _deny(self, reason: str) -> bool:
        self.last = Decision(False, reason)
        return False
