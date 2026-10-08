"""Inbound poke controller for bounded native poke interactions."""

from __future__ import annotations

import os
import random
import time
import logging
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..domain import MessageEvent

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PokeSettings:
    enabled: bool = True
    counter_probability: float = 0.50
    cooldown_seconds: float = 25.0
    daily_user_limit: int = 8
    follow_window_seconds: float = 45.0
    follow_distinct_users: int = 3
    follow_probability: float = 0.75
    follow_cooldown_seconds: float = 180.0

    @classmethod
    def from_env(cls) -> "PokeSettings":
        def number(name: str, default: float) -> float:
            try:
                return max(0.0, min(1.0, float(os.getenv(name, str(default)))))
            except ValueError:
                return default
        return cls(
            enabled=os.getenv("BOT_POKE_ENABLED", "true").lower() == "true",
            counter_probability=number("BOT_POKE_COUNTER_PROBABILITY", .50),
            cooldown_seconds=max(
                0.0, float(os.getenv("BOT_POKE_COOLDOWN_SECONDS", "25"))
            ),
            daily_user_limit=max(
                0, int(os.getenv("BOT_POKE_DAILY_USER_LIMIT", "8"))
            ),
            follow_window_seconds=max(5, float(os.getenv("BOT_POKE_FOLLOW_WINDOW_SECONDS", "45"))),
            follow_distinct_users=max(2, int(os.getenv("BOT_POKE_FOLLOW_DISTINCT_USERS", "3"))),
            follow_probability=number("BOT_POKE_FOLLOW_PROBABILITY", .75),
            follow_cooldown_seconds=max(0, float(os.getenv("BOT_POKE_FOLLOW_COOLDOWN_SECONDS", "180"))),
        )


class PokeController:
    def __init__(
        self, settings: PokeSettings, service, sender, allowed_groups: frozenset[str],
        *, random_value: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
    ):
        self.settings, self.service, self.sender = settings, service, sender
        self.allowed_groups = allowed_groups
        self.random_value, self.clock, self.today = random_value, clock, today
        self._last: dict[tuple[str, str], float] = {}
        self._counts: Counter[tuple[str, str]] = Counter()
        self._poke_crowds: dict[tuple[str, str], deque[tuple[float, str]]] = defaultdict(deque)
        self._last_follow: dict[tuple[str, str], float] = {}
        self._day: date | None = None

    async def handle(self, raw: dict) -> bool:
        if not self._is_poke_notice(raw):
            return False
        group = str(raw.get("group_id") or "")
        user = str(raw.get("user_id") or "")
        target = str(raw.get("target_id") or "")
        self_id = str(raw.get("self_id") or "")
        if not self.settings.enabled or group not in self.allowed_groups or not user:
            return True
        if target != self_id:
            await self._maybe_follow_crowd(group, user, target)
            return True
        key = (group, user)
        now = self.clock()
        current_day = self.today()
        if current_day != self._day:
            self._day = current_day
            self._counts.clear()
        if (
            now - self._last.get(key, float("-inf")) < self.settings.cooldown_seconds
            or self._counts[key] >= self.settings.daily_user_limit
        ):
            return True
        self._last[key] = now
        self._counts[key] += 1
        event = MessageEvent(
            event_id=f"poke:{group}:{user}:{int(raw.get('time') or now)}",
            scope=f"group:{group}", group_id=group, user_id=user,
            nickname=f"群友{user[-4:]}", text="[戳一戳] 戳了戳你",
            image_urls=(), at_bot=True, at_users=(),
            timestamp=int(raw.get("time") or now),
        )
        if not await self.service.ingest(event):
            return True
        if self.random_value() < self.settings.counter_probability:
            try:
                await self.sender.poke_group(group, user)
                self.service.record_assistant_turn(
                    event, f"[戳一戳] 戳了戳群友{user[-4:]}",
                    event_id=f"poke-reply:{event.event_id}",
                )
            except Exception:
                log.exception("Counter-poke failed group=%s user=%s", group, user)
        return True

    async def _maybe_follow_crowd(self, group: str, user: str, target: str) -> None:
        if not target or target == user:
            return
        now = self.clock()
        key = (group, target)
        crowd = self._poke_crowds[key]
        crowd.append((now, user))
        while crowd and now - crowd[0][0] > self.settings.follow_window_seconds:
            crowd.popleft()
        distinct = {actor for _, actor in crowd}
        if len(distinct) < self.settings.follow_distinct_users:
            return
        if now - self._last_follow.get(key, float("-inf")) < self.settings.follow_cooldown_seconds:
            return
        if self.random_value() >= self.settings.follow_probability:
            return
        try:
            await self.sender.poke_group(group, target)
            self._last_follow[key] = now
            crowd.clear()
            log.info("Followed poke crowd group=%s target=%s", group, target)
        except Exception:
            log.exception("Crowd follow-poke failed group=%s target=%s", group, target)

    @staticmethod
    def _is_poke_notice(raw: dict) -> bool:
        return (
            raw.get("post_type") == "notice"
            and raw.get("notice_type") == "notify"
            and raw.get("sub_type") == "poke"
            and raw.get("group_id") is not None
        )
