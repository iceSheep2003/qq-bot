"""Model-free repeat and tiny-game decisions for QQ group chat."""

from __future__ import annotations

import os
import random
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..content import is_media_placeholder
from ..domain import MessageEvent


def _number(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


@dataclass(frozen=True)
class LightInteractionSettings:
    repeat_probability: float = 0.68
    repeat_cooldown_seconds: float = 90.0
    daily_group_limit: int = 20
    repeat_content_cooldown_seconds: int = 21600

    @classmethod
    def from_env(cls) -> "LightInteractionSettings":
        return cls(
            repeat_probability=_number("BOT_REPEAT_PROBABILITY", 0.68, 0, 1),
            repeat_cooldown_seconds=_number("BOT_REPEAT_COOLDOWN_SECONDS", 90, 0, 3600),
            daily_group_limit=int(_number("BOT_REPEAT_DAILY_GROUP_LIMIT", 20, 0, 200)),
            repeat_content_cooldown_seconds=int(_number("BOT_REPEAT_CONTENT_COOLDOWN_SECONDS", 21600, 0, 604800)),
        )


@dataclass(frozen=True)
class LightAction:
    kind: str = ""
    text: str = ""
    reason: str = ""
    source_key: str = ""

    @property
    def active(self) -> bool:
        return bool(self.kind)


class LightInteractionPolicy:
    def __init__(
        self,
        settings: LightInteractionSettings = LightInteractionSettings(),
        *, random_value: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
        repeat_ledger=None,
    ):
        self.settings = settings
        self.random_value = random_value
        self.clock = clock
        self.today = today
        self.repeat_ledger = repeat_ledger
        self._last_repeat: dict[str, float] = {}
        self._counts: Counter[str] = Counter()
        self._day: date | None = None

    def decide(self, event: MessageEvent, recent: list[dict]) -> LightAction:
        if not event.group_id:
            return LightAction()
        command = self._normalize(event.text)
        if event.at_bot and command in {"掷骰子", "扔骰子", "骰子", "roll", "roll点"}:
            return LightAction("dice", reason="explicit dice request")
        if event.at_bot and command in {"猜拳", "石头剪刀布", "划拳"}:
            return LightAction("rps", reason="explicit rps request")
        if event.at_bot or event.reply_to_message_id:
            return LightAction()
        candidate, source_key = self._repeat_candidate_details(recent)
        if not candidate:
            return LightAction()
        self._roll_day()
        cfg = self.settings
        now = self.clock()
        if self._counts[event.group_id] >= cfg.daily_group_limit:
            return LightAction()
        if now - self._last_repeat.get(event.group_id, float("-inf")) < cfg.repeat_cooldown_seconds:
            return LightAction()
        if self.random_value() >= cfg.repeat_probability:
            return LightAction()
        if self.repeat_ledger is not None and not self.repeat_ledger.claim_repeat(
            event.group_id, source_key, self._normalize(candidate), now=int(now),
            cooldown_seconds=int(cfg.repeat_cooldown_seconds),
            content_cooldown_seconds=cfg.repeat_content_cooldown_seconds,
            daily_limit=cfg.daily_group_limit,
        ):
            return LightAction()
        self._counts[event.group_id] += 1
        self._last_repeat[event.group_id] = now
        return LightAction("repeat", candidate, "two distinct members repeated", source_key)

    @classmethod
    def _repeat_candidate(cls, recent: list[dict]) -> str:
        return cls._repeat_candidate_details(recent)[0]

    @classmethod
    def _repeat_candidate_details(cls, recent: list[dict]) -> tuple[str, str]:
        users = [row for row in recent if row.get("role") == "user"][-2:]
        if len(users) < 2:
            return "", ""
        marker = cls._normalize(str(users[-1].get("content") or ""))
        # Repetition is opt-in and deliberately strict: only the exact
        # normalized marker ``+1`` asks the bot to join in. Ordinary identical
        # messages, “复读”, “跟” and other variants must never trigger it.
        if marker != "+1":
            return "", ""
        raw = str(users[-2].get("content") or "").strip()
        candidate = cls._normalize(raw)
        if not candidate or len(raw) < 2 or len(raw) > 30:
            return "", ""
        # Image-only and other media messages are stored as transcript markers.
        # Repeating one as text would make QQ display a literal ``[图片]``.
        if is_media_placeholder(raw):
            return "", ""
        if len({str(row.get("user_id")) for row in users}) != 2:
            return "", ""
        if "http" in raw.lower() or any(char.isdigit() for char in raw) and len(raw) > 12:
            return "", ""
        # SQLite row IDs are stable across restarts and distinguish a genuine
        # later repetition of the same phrase from this already-used pair.
        ids = [str(row.get("id") or row.get("event_id") or "") for row in users]
        import hashlib
        # Production rows always have stable SQLite IDs. Synthetic callers and
        # older integrations may not; retain a deterministic conservative key.
        identity = ids if all(ids) else [candidate, *(str(row.get("user_id") or "") for row in users)]
        source_key = hashlib.sha256("\0".join(identity).encode()).hexdigest()
        return raw, source_key

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"[\s，。！？!?、~～]+", "", text).lower()

    def _roll_day(self) -> None:
        current = self.today()
        if current != self._day:
            self._day = current
            self._counts.clear()
