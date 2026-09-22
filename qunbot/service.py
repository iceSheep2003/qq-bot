"""Application use cases. No NapCat, HTTP or SQLite imports."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .domain import JobSkipped, MessageEvent
from .media import strip_markers
from .ports import (
    ActivityRepository,
    AffectionObserver,
    AgentRunner,
    ConversationRepository,
    MediaProcessor,
    MessageSender,
    PosterRenderer,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotPolicy:
    allowed_groups: frozenset[str]
    memory_extract_every: int
    proactive_enabled: bool
    proactive_interval_minutes: int
    proactive_daily_limit: int
    timezone: str
    affection_auto_enabled: bool
    private_enabled: bool = False
    # Scheduled posts get their own daily quota so that random proactive chat
    # and cron jobs cannot starve each other.
    job_daily_limit: int = 6
    active_start_hour: int = 7
    active_end_hour: int = 23
    job_cooldown_minutes: int = 30
    # "Group is not dead", not "someone spoke a minute ago" — a study group is
    # often silent for a while and a scheduled post should still land.
    job_freshness_minutes: int = 180


class ConversationService:
    def __init__(
        self,
        agent: AgentRunner,
        conversations: ConversationRepository,
        activity: ActivityRepository,
        sender: MessageSender,
        policy: BotPolicy,
        affection: AffectionObserver | None = None,
        media: MediaProcessor | None = None,
        posters: PosterRenderer | None = None,
    ):
        self.agent, self.conversations, self.activity = agent, conversations, activity
        self.sender, self.policy = sender, policy
        self.affection = affection
        self.media = media
        self.posters = posters
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
        async with self.scope_locks.setdefault(event.scope, asyncio.Lock()):
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

    async def _extract_safe(self, scope: str) -> None:
        try:
            async with self.extract_slots:
                await self.agent.extract_memory(scope)
        except Exception:
            log.exception("Memory extraction failed for %s", scope)
        finally:
            self.extracting.discard(scope)

    def _job_event(self, job: dict, now: int) -> MessageEvent:
        """Wrap a schedule prompt as the pseudo-message the Agent answers."""
        group_id = job["group_id"]
        return MessageEvent(
            event_id=f"job:{job['id']}:{job['run_id']}",
            scope=f"group:{group_id}",
            group_id=group_id,
            user_id="bot",
            nickname="Bot",
            text=job["prompt"],
            image_urls=(),
            at_bot=False,
            at_users=(),
            timestamp=now,
        )

    async def run_job(self, job: dict) -> None:
        """Execute one schedule. Raises JobSkipped for expected no-ops."""
        group_id = job["group_id"]
        if group_id not in self.policy.allowed_groups:
            raise JobSkipped("group is not allowlisted")
        if not self.within_active_hours():
            raise JobSkipped("outside active hours")
        if job.get("action") == "poster":
            await self._run_poster_job(job)
        else:
            await self._run_chat_job(job)

    async def _run_poster_job(self, job: dict) -> None:
        """Send the countdown poster. The image is drawn locally, so a model
        outage costs the caption but never the daily poster."""
        if self.posters is None:
            raise JobSkipped("no poster configured (set BOT_EXAM_DATE)")
        group_id = job["group_id"]
        today = self.local_today()
        event = self._job_event(job, int(time.time()))
        async with self.scope_locks.setdefault(event.scope, asyncio.Lock()):
            caption = await self._poster_caption(job, event)
            poster = self.posters.render(today, motto=caption[:24])
            if poster is None:
                raise JobSkipped("countdown already finished")
            days = self.posters.days_left(today)
            text = caption[:300] or f"距离考研还有 {days} 天，继续加油。"
            await self.sender.send(
                group_id=group_id, user_id=None, text=text, image=poster
            )
            self.activity.log_proactive(group_id, text, "poster")
            self.conversations.add_message(
                event.event_id, event.scope, "bot", "Bot", "assistant", text
            )
            self.last_reply[event.scope] = time.time()

    async def _poster_caption(self, job: dict, event: MessageEvent) -> str:
        if not job.get("prompt"):
            return ""
        try:
            reply = await self.agent.reply(event, proactive=True)
        except Exception:
            log.exception("Poster caption failed for job %s", job["id"])
            return ""
        return strip_markers(reply.text or "")

    async def _run_chat_job(self, job: dict) -> None:
        group_id, scope = job["group_id"], f"group:{job['group_id']}"
        if (
            self.activity.proactive_count_since(group_id, self.today_start(), "cron")
            >= self.policy.job_daily_limit
        ):
            raise JobSkipped("daily scheduled-post limit reached")
        now = int(time.time())
        if now - self.last_reply.get(scope, 0) < self.policy.job_cooldown_minutes * 60:
            raise JobSkipped("bot replied to this group recently")
        recent = self.conversations.recent(scope, 12)
        human = [row for row in recent if row["role"] == "user"]
        if (
            not human
            or now - human[-1]["created_at"] > self.policy.job_freshness_minutes * 60
        ):
            raise JobSkipped("group went quiet")
        event = self._job_event(job, now)
        async with self.scope_locks.setdefault(scope, asyncio.Lock()):
            reply = await self.agent.reply(event, proactive=True)
            text = (reply.text or "").strip()
            if not text:
                raise JobSkipped("model chose to stay silent")
            if len(text) > 150 or any(text == row["content"] for row in recent):
                raise JobSkipped("reply too long or repeated")
            clean = await self.send_reply(group_id, None, text)
            if clean:
                self.activity.log_proactive(group_id, clean, "cron")
                self.conversations.add_message(
                    event.event_id, scope, "bot", "Bot", "assistant", clean
                )
                self.last_reply[scope] = time.time()

    async def maybe_proactive(self, group_id: str) -> None:
        if (
            not self.policy.proactive_enabled
            or group_id not in self.policy.allowed_groups
            or not self.within_active_hours()
        ):
            return
        scope, now = f"group:{group_id}", time.time()
        if (
            self.activity.proactive_count_since(group_id, self.today_start(), "random")
            >= self.policy.proactive_daily_limit
        ):
            return
        if (
            now - self.activity.last_proactive(group_id)
            < self.policy.proactive_interval_minutes * 60
        ):
            return
        if now - self.last_reply.get(scope, 0) < 30 * 60:
            return
        recent = self.conversations.recent(scope, 12)
        human = [r for r in recent if r["role"] == "user"]
        if (
            len(human) < 3
            or now - human[-1]["created_at"] > 20 * 60
            or random.random() > 0.15
        ):
            return
        event = MessageEvent(
            event_id=f"proactive:{group_id}:{int(now)}",
            scope=scope,
            group_id=group_id,
            user_id="bot",
            nickname="Bot",
            text="结合最近群聊，若有自然切入点就简短接一句；否则不发言。",
            image_urls=(),
            at_bot=False,
            at_users=(),
            timestamp=int(now),
        )
        async with self.scope_locks.setdefault(scope, asyncio.Lock()):
            reply = await self.agent.reply(event, proactive=True)
            if (
                not reply.text
                or len(reply.text) > 150
                or any(reply.text == r["content"] for r in recent)
            ):
                return
            clean = await self.send_reply(group_id, None, reply.text)
            if clean:
                self.activity.log_proactive(group_id, clean, "random")
                self.conversations.add_message(
                    event.event_id, scope, "bot", "Bot", "assistant", clean
                )
                self.last_reply[scope] = time.time()
