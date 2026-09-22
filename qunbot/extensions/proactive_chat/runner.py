"""Proactive continuation and its own gates, outside the conversation core."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Callable

from ...domain import MessageEvent
from ...ports import MoodObserver

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProactiveConfig:
    interval_minutes: int = 180
    daily_limit: int = 2

    @classmethod
    def from_env(cls) -> ProactiveConfig:
        return cls(
            max(15, int(os.getenv("BOT_PROACTIVE_INTERVAL_MINUTES", "180"))),
            max(0, int(os.getenv("BOT_PROACTIVE_DAILY_LIMIT", "2"))),
        )


class ProactiveChat:
    def __init__(self, bot, config: ProactiveConfig, mood: MoodObserver | None = None):
        self.bot, self.config, self.mood = bot, config, mood

    async def maybe_post(self, group_id: str) -> None:
        bot = self.bot
        if group_id not in bot.policy.allowed_groups or not bot.within_active_hours():
            return
        scope, now = f"group:{group_id}", time.time()
        if self.mood and not self.mood.permits_proactive(scope):
            return
        if bot.activity.proactive_count_since(group_id, bot.today_start(), "random") >= self.config.daily_limit:
            return
        if now - bot.activity.last_proactive(group_id) < self.config.interval_minutes * 60:
            return
        if now - bot.last_reply.get(scope, 0) < 30 * 60:
            return
        recent = bot.conversations.recent(scope, 12)
        human = [row for row in recent if row["role"] == "user"]
        if len(human) < 3 or now - human[-1]["created_at"] > 20 * 60 or random.random() > 0.15:
            return
        event = MessageEvent(
            f"proactive:{group_id}:{int(now)}", scope, group_id, "bot", "Bot",
            "结合最近群聊，若有自然切入点就简短接一句；否则不发言。",
            (), False, (), int(now),
        )
        async with bot.scope_lock(scope):
            reply = await bot.agent.reply(event, proactive=True)
            if not reply.text or len(reply.text) > 150 or any(reply.text == row["content"] for row in recent):
                return
            clean = await bot.send_reply(group_id, None, reply.text)
            if clean:
                bot.activity.log_proactive(group_id, clean, "random")
                bot.conversations.add_message(event.event_id, scope, "bot", "Bot", "assistant", clean)
                bot.last_reply[scope] = time.time()

    async def loop(self, connected: Callable[[], bool]) -> None:
        while True:
            await asyncio.sleep(60)
            if not connected():
                continue
            for group_id in self.bot.policy.allowed_groups:
                try:
                    await self.maybe_post(group_id)
                except Exception:
                    log.exception("Proactive check failed for group %s", group_id)


def build_worker(bot, _gateway, _config, mood):
    return ProactiveChat(bot, ProactiveConfig.from_env(), mood).loop(
        lambda: bool(_gateway.connection)
    )
