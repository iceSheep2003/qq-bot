"""Water the group: one short contextual line, or nothing at all."""

from __future__ import annotations

import time

from ...scheduling import guards
from ...scheduling.registry import JobRuntime, JobSuggestion, suggestion


class ChatJobHandler:
    action = "chat"

    def suggested_jobs(self) -> list[JobSuggestion]:
        return [
            suggestion(
                "water-noon",
                "30 12 * * *",
                "午休时间，结合最近群里聊过的话题随口接一句。要像群友闲聊，"
                "不要像通知。如果实在没有合适的切入点，就什么都不要说。",
                description="午休闲聊",
            ),
            suggestion(
                "water-night",
                "30 22 * * *",
                "快收工了，结合最近群里的聊天说一句轻松的话收尾。别喊口号，"
                "像朋友随口说的那种。",
                description="晚间收尾",
            ),
        ]

    async def run(self, bot: JobRuntime, job: dict) -> None:
        policy, group_id = bot.policy, job["group_id"]
        scope = f"group:{group_id}"
        guards.enforce_daily_limit(
            bot, group_id, source="cron", limit=policy.job_daily_limit
        )
        guards.enforce_cooldown(bot, scope)
        recent = guards.enforce_group_awake(bot, scope)
        event = bot.job_event(job, int(time.time()))
        async with bot.scope_lock(scope):
            reply = await bot.agent.reply(event, proactive=True)
            guards.enforce_fresh_text(
                (reply.text or "").strip(), recent, max_chars=policy.job_max_chars
            )
            clean = await bot.send_reply(group_id, None, reply.text.strip())
            if clean:
                guards.record(bot, job, event, clean, "cron")


def register_jobs(registry, _config) -> None:
    registry.register(ChatJobHandler())
