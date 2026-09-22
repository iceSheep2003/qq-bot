"""Reusable pre-post checks.

These are the building blocks a scheduled task composes when it should behave
like a group member rather than a broadcast channel. Each one raises JobSkipped
with a human-readable reason, so the run shows up as ``skipped`` in job_runs
and the reason lands in the log.

Group allowlist and active hours apply to every task and are enforced centrally
by JobRunner.run before the handler is called.
"""

from __future__ import annotations

import time

from ..domain import JobSkipped, MessageEvent
from .registry import JobRuntime


def enforce_daily_limit(
    bot: JobRuntime, group_id: str, *, source: str, limit: int
) -> None:
    """Cap posts per group per day, counted per source pool."""
    if bot.activity.proactive_count_since(group_id, bot.today_start(), source) >= limit:
        raise JobSkipped(f"daily {source} limit reached ({limit})")


def enforce_cooldown(bot: JobRuntime, scope: str) -> None:
    """Do not talk over a reply the bot just sent to this group."""
    elapsed = time.time() - bot.last_reply.get(scope, 0)
    if elapsed < bot.policy.job_cooldown_minutes * 60:
        raise JobSkipped("bot replied to this group recently")


def enforce_group_awake(bot: JobRuntime, scope: str) -> list[dict]:
    """Require the group to have been alive recently. Returns the recent rows.

    This asks "is this group not dead", not "did someone speak a minute ago":
    a study group is often quiet and a scheduled post should still land.
    """
    now = int(time.time())
    recent = bot.conversations.recent(scope, 12)
    human = [row for row in recent if row["role"] == "user"]
    if not human:
        raise JobSkipped("group has never spoken")
    if now - human[-1]["created_at"] > bot.policy.job_freshness_minutes * 60:
        raise JobSkipped("group went quiet")
    return recent


def enforce_fresh_text(text: str, recent: list[dict], *, max_chars: int) -> None:
    """Reject a reply that is too long or that repeats the recent history."""
    if not text:
        raise JobSkipped("model chose to stay silent")
    if len(text) > max_chars:
        raise JobSkipped(f"reply longer than {max_chars} characters")
    if any(text == row["content"] for row in recent):
        raise JobSkipped("reply repeated a recent message")


def record(
    bot: JobRuntime, job: dict, event: MessageEvent, text: str, source: str
) -> None:
    """Shared bookkeeping: quota log, group history and the reply cooldown."""
    bot.activity.log_proactive(job["group_id"], text, source)
    bot.conversations.add_message(
        event.event_id, event.scope, "bot", "Bot", "assistant", text
    )
    bot.last_reply[event.scope] = time.time()
