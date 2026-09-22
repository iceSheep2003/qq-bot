"""Application use cases. No NapCat, HTTP or SQLite imports."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..domain import MessageEvent
from ..ports import (
    ActivityRepository,
    AffectionObserver,
    AgentRunner,
    ConversationRepository,
    MediaProcessor,
    MessageSender,
    PeopleRepository,
    ReplyObserver,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotPolicy:
    allowed_groups: frozenset[str]
    memory_extract_every: int
    timezone: str
    affection_auto_enabled: bool
    private_enabled: bool = False
    # Scheduled posts have their own quota; optional proactive chat owns its own.
    job_daily_limit: int = 6
    active_start_hour: int = 7
    active_end_hour: int = 23
    job_cooldown_minutes: int = 30
    # "Group is not dead", not "someone spoke a minute ago" — a study group is
    # often silent for a while and a scheduled post should still land.
    job_freshness_minutes: int = 180
    job_max_chars: int = 150


class ConversationService:
    def __init__(
        self,
        agent: AgentRunner,
        conversations: ConversationRepository,
        people: PeopleRepository,
        activity: ActivityRepository,
        sender: MessageSender,
        policy: BotPolicy,
        affection: AffectionObserver | None = None,
        media: MediaProcessor | None = None,
        observers: tuple[ReplyObserver, ...] = (),
    ):
        self.agent, self.conversations, self.activity = agent, conversations, activity
        self.people = people
        self.sender, self.policy = sender, policy
        self.affection = affection
        self.media = media
        self.observers = observers
        self.scope_locks: dict[str, asyncio.Lock] = {}
        self.last_reply: dict[str, float] = {}
        self.extracting: set[str] = set()
        self.extract_slots = asyncio.Semaphore(2)

    def local_hour(self) -> int:
        return datetime.now(ZoneInfo(self.policy.timezone)).hour

    def local_today(self) -> date:
        return datetime.now(ZoneInfo(self.policy.timezone)).date()

    def within_active_hours(self) -> bool:
        return (
            self.policy.active_start_hour
            <= self.local_hour()
            < self.policy.active_end_hour
        )

    def today_start(self) -> int:
        return int(
            datetime.now(ZoneInfo(self.policy.timezone))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

    async def handle_message(self, event: MessageEvent) -> None:
        if not event.group_id and not self.policy.private_enabled:
            return
        if event.group_id and event.group_id not in self.policy.allowed_groups:
            return
        text = event.text or ("[图片]" if event.image_urls else "")
        if not text or not self.conversations.add_message(
            event.event_id, event.scope, event.user_id, event.nickname, "user", text
        ):
            return
        self.people.observe_user(event.user_id, event.nickname)
        if (
            self.conversations.message_count(event.scope)
            % self.policy.memory_extract_every
            == 0
            and event.scope not in self.extracting
        ):
            self.extracting.add(event.scope)
            asyncio.create_task(self._extract_safe(event.scope))
        if event.group_id and not event.at_bot:
            return
        async with self.scope_lock(event.scope):
            try:
                reply = await self.agent.reply(event)
            except Exception:
                log.exception("Model call failed")
                await self.sender.send(
                    group_id=event.group_id,
                    user_id=None if event.group_id else event.user_id,
                    text="我刚才没能完成回复，稍后再试吧。",
                )
                return
            if reply.text:
                clean = await self.send_reply(
                    event.group_id,
                    None if event.group_id else event.user_id,
                    reply.text,
                )
                if not clean:
                    return
                self.conversations.add_message(
                    f"reply:{event.event_id}",
                    event.scope,
                    "bot",
                    "Bot",
                    "assistant",
                    clean,
                )
                self.last_reply[event.scope] = time.time()
                if (
                    self.affection
                    and self.policy.affection_auto_enabled
                    and event.group_id
                ):
                    asyncio.create_task(self._affection_safe(event, clean))
                for observer in self.observers:
                    asyncio.create_task(self._observe_safe(observer, event, clean))

    async def send_reply(
        self, group_id: str | None, user_id: str | None, text: str
    ) -> str:
        clean, image, voice = (
            await self.media.compose(text) if self.media else (text, None, None)
        )
        if clean or image:
            await self.sender.send(
                group_id=group_id, user_id=user_id, text=clean, image=image
            )
        if voice:
            await self.sender.send(group_id=group_id, user_id=user_id, voice=voice)
        return clean

    async def _affection_safe(self, event: MessageEvent, bot_reply: str) -> None:
        try:
            await self.affection.observe(event, bot_reply)
        except Exception:
            log.exception("Affection evaluation failed for %s", event.event_id)

    async def _observe_safe(
        self, observer: ReplyObserver, event: MessageEvent, bot_reply: str
    ) -> None:
        try:
            await observer.observe(event, bot_reply)
        except Exception:
            log.exception("Post-reply observer failed for %s", event.event_id)

    async def _extract_safe(self, scope: str) -> None:
        try:
            async with self.extract_slots:
                await self.agent.extract_memory(scope)
        except Exception:
            log.exception("Memory extraction failed for %s", scope)
        finally:
            self.extracting.discard(scope)

    def scope_lock(self, scope: str) -> asyncio.Lock:
        return self.scope_locks.setdefault(scope, asyncio.Lock())
