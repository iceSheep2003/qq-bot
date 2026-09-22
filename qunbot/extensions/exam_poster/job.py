"""Post a locally rendered image, optionally with a model-written caption.

The image is drawn locally, so a model outage costs the caption but never the
poster itself. This is the smallest example of a handler that is not a chat
message: it skips the chat guards entirely because a scheduled poster is a
deliverable, not small talk.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date

from ...domain import JobSkipped, MessageEvent
from ..media import strip_markers
from ...scheduling import guards
from ...scheduling.registry import JobRuntime, JobSuggestion, suggestion

log = logging.getLogger(__name__)

MOTTO_CHARS = 24
CAPTION_CHARS = 300


class PosterJobHandler:
    def __init__(self, renderer=None):
        self.renderer = renderer
    action = "poster"

    def suggested_jobs(self) -> list[JobSuggestion]:
        return [
            suggestion(
                "kaoyan-daily-poster",
                "0 7 * * *",
                "这是今天考研倒计时海报的配文。用一句话给正在备考的同学打气，"
                "20 字以内，直接给出句子本身，不要引号、不要 emoji、不要任何解释。",
                description="考研倒计时海报（需要 BOT_EXAM_DATE）",
            )
        ]

    async def run(self, bot: JobRuntime, job: dict) -> None:
        if self.renderer is None:
            raise JobSkipped("no poster renderer configured (set BOT_EXAM_DATE)")
        today = bot.local_today()
        event = bot.job_event(job, int(time.time()))
        async with bot.scope_lock(event.scope):
            caption = await self._caption(bot, job, event)
            poster = self.renderer.render(today, motto=caption[:MOTTO_CHARS])
            if poster is None:
                raise JobSkipped("countdown already finished")
            days = self.renderer.days_left(today)
            text = caption[:CAPTION_CHARS] or f"距离考研还有 {days} 天，继续加油。"
            await bot.sender.send(
                group_id=job["group_id"], user_id=None, text=text, image=poster
            )
            guards.record(bot, job, event, text, "poster")

    async def _caption(self, bot: JobRuntime, job: dict, event: MessageEvent) -> str:
        """A model outage must not cost the scheduled image."""
        if not job.get("prompt"):
            return ""
        try:
            reply = await bot.agent.reply(event, proactive=True)
        except Exception:
            log.exception("Poster caption failed for job %s", job["id"])
            return ""
        return strip_markers(reply.text or "")


def register_jobs(registry, _config) -> None:
    raw_date = os.getenv("BOT_EXAM_DATE", "").strip()
    if not raw_date:
        raise ValueError("BOT_EXAM_DATE is required for exam_poster")
    from .renderer import ExamCountdownPoster

    registry.register(
        PosterJobHandler(
            ExamCountdownPoster(
                date.fromisoformat(raw_date), os.getenv("BOT_POSTER_FONT", "").strip()
            )
        )
    )
