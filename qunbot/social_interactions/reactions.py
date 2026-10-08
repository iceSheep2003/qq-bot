"""Select a restrained QQ message reaction from local conversational cues."""

from __future__ import annotations

import os
import random
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import date
from typing import Callable

from ..domain import MessageEvent

# Reaction IDs are owner-controlled platform data. The model never sees or
# supplies them. These conservative built-in face IDs are also supported by
# NapCat's ordinary QQ face converter.
REACTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("胜利", "78", ("完成", "搞定", "过了", "上岸", "成功", "做完", "背完", "学完")),
    ("赞", "76", ("厉害", "牛", "不错", "可以", "稳", "支持", "恭喜", "真棒", "优秀")),
    ("呲牙", "13", ("哈哈", "笑死", "绷不住", "乐", "逆天", "草", "6", "666")),
    ("微笑", "14", ("早安", "晚安", "谢谢", "感谢", "收到", "好耶", "开心")),
    ("流泪", "5", ("呜呜", "哭了", "难过", "崩溃", "寄了", "没过", "失败")),
)

_BLOCKED = ("密码", "验证码", "身份证", "银行卡", "手机号", "自杀", "去死")


def _boolean(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _number(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


@dataclass(frozen=True)
class ReactionSettings:
    enabled: bool = True
    probability: float = 0.65
    cooldown_seconds: float = 4.0
    daily_group_limit: int = 80
    daily_user_limit: int = 20

    @classmethod
    def from_env(cls) -> "ReactionSettings":
        return cls(
            enabled=_boolean("BOT_REACTION_ENABLED", True),
            probability=_number("BOT_REACTION_PROBABILITY", 0.65, 0.0, 1.0),
            cooldown_seconds=_number("BOT_REACTION_COOLDOWN_SECONDS", 4.0, 0.0, 300.0),
            daily_group_limit=int(_number("BOT_REACTION_DAILY_GROUP_LIMIT", 80, 0, 1000)),
            daily_user_limit=int(_number("BOT_REACTION_DAILY_USER_LIMIT", 20, 0, 200)),
        )


@dataclass(frozen=True)
class ReactionDecision:
    emoji_id: str = ""
    name: str = ""
    reason: str = ""

    @property
    def react(self) -> bool:
        return bool(self.emoji_id)


class ReactionPolicy:
    """Stateful admission policy for high-frequency, low-noise reactions."""

    def __init__(
        self,
        settings: ReactionSettings = ReactionSettings(),
        *,
        random_value: Callable[[], float] = random.random,
        choose: Callable[[list[tuple[str, str]]], tuple[str, str]] = random.choice,
        clock: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
        dedupe_size: int = 4096,
    ):
        self.settings = settings
        self.random_value = random_value
        self.choose = choose
        self.clock = clock
        self.today = today
        self.dedupe_size = max(1, dedupe_size)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._last_group: dict[str, float] = {}
        self._day: date | None = None
        self._groups: Counter[str] = Counter()
        self._users: Counter[tuple[str, str]] = Counter()

    def decide(self, event: MessageEvent) -> ReactionDecision:
        cfg = self.settings
        if not cfg.enabled or not event.group_id or not event.platform_message_id:
            return ReactionDecision(reason="disabled or unsupported event")
        if event.event_id in self._seen:
            return ReactionDecision(reason="already considered")
        self._remember(event.event_id)
        self._roll_day()
        if cfg.daily_group_limit <= self._groups[event.group_id]:
            return ReactionDecision(reason="group daily limit")
        user_key = (event.group_id, event.user_id)
        if cfg.daily_user_limit <= self._users[user_key]:
            return ReactionDecision(reason="user daily limit")
        now = self.clock()
        if now - self._last_group.get(event.group_id, float("-inf")) < cfg.cooldown_seconds:
            return ReactionDecision(reason="group cooldown")

        text = " ".join((event.text or "").lower().split())
        if not text or any(word in text for word in _BLOCKED):
            return ReactionDecision(reason="no safe social cue")
        matches: list[tuple[str, str]] = []
        for name, emoji_id, keywords in REACTIONS:
            if any(keyword.lower() in text for keyword in keywords):
                matches.append((name, emoji_id))
        if not matches:
            return ReactionDecision(reason="no semantic match")
        # A direct address can already receive a full reply. Reactions remain
        # possible, but at a slightly lower rate so every answer is not
        # mechanically decorated with one.
        probability = cfg.probability * (0.72 if event.at_bot else 1.0)
        if self.random_value() >= probability:
            return ReactionDecision(reason="probability gate")
        name, emoji_id = self.choose(matches)
        self._last_group[event.group_id] = now
        self._groups[event.group_id] += 1
        self._users[user_key] += 1
        return ReactionDecision(emoji_id, name, "local semantic cue")

    def _remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        self._seen.move_to_end(event_id)
        while len(self._seen) > self.dedupe_size:
            self._seen.popitem(last=False)

    def _roll_day(self) -> None:
        current = self.today()
        if current != self._day:
            self._day = current
            self._groups.clear()
            self._users.clear()
