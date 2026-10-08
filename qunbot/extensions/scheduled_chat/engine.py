"""AIReplay-style continuation: "X minutes after the last message".

Shared by both trigger paths: the scheduler's ``continuation`` action in
``job.py`` (the canonical one, which also records skipped/failed runs) and the
``proactive_chat`` background worker, which imports this module. It lives here
with the action that owns it because ``extensions/loader.py`` only lets a
``JOB_EXTENSIONS`` entry register a job — so enabling ``scheduled_chat`` never
pulls in the background extension, and the two cannot drift apart.

This is not a dice roll on a timer. The group falls quiet first; then, once per
tick, the bot asks itself whether this particular quiet window is one where it
has something to say. Every answer — spoken or silent — is written down, and
the window's state (already answered? how many silent decisions so far?) is
persisted, so a restart resumes the same conversation rather than starting a
fresh round of dice.

The order of the gates matters, because it decides what a skip *means*:

1. Deployment gates (allowlist, active hours, extension switch, group mute,
   per-group quiet hours) — the bot was never going to speak, so nothing is
   written against the window.
2. Window gates (enough history, still talking, window already answered, stale
   window) — the group, not the bot, decides these; still no attempt counted.
3. Turn gates (mood, daily quota, post cooldown, the probability roll) and the
   model's own verdict — these are attempts, so they count, lengthen the wait
   and show up in the decision log.

Nothing here writes to the stable persona, the affection store or a task; it
only reads the room and, at most, sends one short message.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from ...domain import MessageEvent
from ...content import is_media_placeholder
from ...replies.repetition import repeats_recent
from ...storage.continuation import ContinuationStore
from .policy import GroupPolicySet, ProactiveConfig, in_quiet_hours

log = logging.getLogger(__name__)

DEFAULT_PROMPT = (
    "结合最近群里的聊天，如果你确实有自然、简短的切入点和想说的内容，"
    "就接一句；如果没有合适的，就不要发言。"
)


@dataclass(frozen=True)
class Decision:
    """What this attempt concluded. ``posted`` is the only success."""

    posted: bool
    reason: str
    text: str = ""


def local_hour(bot) -> int:
    """The deployer's wall clock, from whichever surface the bot exposes."""
    reader = getattr(bot, "local_hour", None)
    if callable(reader):
        return int(reader())
    zone = getattr(getattr(bot, "policy", None), "timezone", "UTC")
    return datetime.now(ZoneInfo(zone)).hour


def mood_gate(bot):
    """Find the mood observer, if this deployment has one.

    ``JobRuntime`` deliberately carries no mood capability, so the observer is
    looked up by duck typing on the conversation service the runner already
    holds. A deployment without ``mood`` simply has no gate. Wiring a
    ``proactive_gate`` field onto ``JobRuntime`` would remove this lookup; see
    the delivery notes.
    """
    conversation = getattr(bot, "conversation", bot)
    for observer in getattr(conversation, "observers", ()) or ():
        permits = getattr(observer, "permits_proactive", None)
        if callable(permits):
            return observer
    return None


class ProactiveChat:
    """The continuation engine. ``maybe_post`` decides one group, one tick."""

    def __init__(
        self,
        bot,
        config: ProactiveConfig,
        mood=None,
        policies: GroupPolicySet | None = None,
    ):
        self.bot = bot
        self.config = config
        self.mood = mood
        self.policies = policies if policies is not None else GroupPolicySet()
        self.store = ContinuationStore(bot.conversations)

    # --- the one decision -------------------------------------------------

    async def maybe_post(self, group_id: str, *, prompt: str | None = None) -> Decision:
        """Evaluate one quiet window. Never raises for an expected skip."""
        bot = self.bot
        scope = f"group:{group_id}"
        now = int(time.time())
        if not self.config.enabled:
            return self._not_now(group_id, scope, now, "proactive chat is switched off")
        if group_id not in bot.policy.allowed_groups:
            return self._not_now(group_id, scope, now, "group is not allowlisted")
        within = getattr(bot, "within_active_hours", None)
        if callable(within) and not within():
            return self._not_now(group_id, scope, now, "outside active hours")
        policy = self.policies.resolve(group_id, self.config)
        if policy is None:
            return self._not_now(group_id, scope, now, "group is muted")
        if in_quiet_hours(policy.quiet_start_hour, policy.quiet_end_hour, local_hour(bot)):
            return self._not_now(
                group_id, scope, now, "inside the group's do-not-disturb hours"
            )
        recent = bot.conversations.recent(scope, 24)
        human = [row for row in recent if row["role"] == "user"]
        if len(human) < policy.min_messages:
            return self._not_now(group_id, scope, now, "group has not spoken enough")
        latest_text = str(human[-1].get("content") or "").strip()
        if (
            is_media_placeholder(latest_text)
            or latest_text.startswith("[戳一戳]")
            or latest_text == "+1"
        ):
            return self._not_now(group_id, scope, now, "latest event is not a conversational turn")

        # The conversation table is the clock: the newest human message opens
        # the quiet window this decision is about.
        last_message_at = int(human[-1]["created_at"])
        idle = now - last_message_at
        state = self.store.state(scope)
        closed_at = state["window_closed_at"]
        # A window stays answered until somebody speaks after it was closed.
        # Comparing timestamps rather than clearing a flag on ingest keeps the
        # conversation table the only clock and costs no write per message.
        # Timestamps are whole seconds, so "at the same second" counts as a new
        # message — the harmless direction, since the group must still fall
        # quiet again before anything is said.
        if closed_at is not None and last_message_at < closed_at:
            return self._not_now(
                group_id, scope, now, "waiting for the group to speak again"
            )
        if idle < policy.quiet_minutes * 60:
            return self._not_now(group_id, scope, now, "group is still talking")
        if idle > policy.freshness_minutes * 60:
            # Abandon the window for good: the chatter is too old to continue
            # from, and re-deciding it every tick would be noise.
            self.store.close_window(scope, group_id, now, "stale")
            return Decision(False, "quiet window went stale")
        if now - int(state["last_attempt_at"]) < self._retry_seconds(
            policy, state, last_message_at
        ):
            return self._not_now(
                group_id, scope, now, "already considered this quiet window"
            )

        # --- past here it is a real turn, and it counts as an attempt ---
        if self.mood is not None and not self.mood.permits_proactive(scope):
            return self._attempt(
                group_id, scope, now, "not in the mood to start a conversation"
            )
        if (
            bot.activity.proactive_count_since(
                group_id, bot.today_start(), "random"
            )
            >= policy.daily_limit
        ):
            return self._attempt(group_id, scope, now, "daily proactive limit reached")
        spoken = max(
            float(bot.activity.last_proactive(group_id)),
            float(bot.last_reply.get(scope, 0)),
        )
        if now - spoken < policy.interval_minutes * 60:
            return self._attempt(
                group_id, scope, now, "the bot spoke to this group too recently"
            )
        if random.random() > policy.probability:
            return self._attempt(group_id, scope, now, "the dice said stay quiet")

        event = MessageEvent(
            f"proactive:{group_id}:{now}",
            scope,
            group_id,
            "bot",
            "Bot",
            prompt or DEFAULT_PROMPT,
            (),
            False,
            (),
            now,
            origin="operator",
        )
        async with bot.scope_lock(scope):
            reply = await bot.agent.reply(event, proactive=True)
            text = (reply.text or "").strip()
            if not text:
                return self._attempt(
                    group_id, scope, now, "the model chose to stay silent"
                )
            if len(text) > policy.max_chars:
                return self._attempt(
                    group_id,
                    scope,
                    now,
                    f"reply longer than {policy.max_chars} characters",
                )
            if repeats_recent(text, recent):
                return self._attempt(
                    group_id, scope, now, "reply repeated a recent message"
                )
            clean = await bot.send_reply(group_id, None, text)
            if not clean:
                return self._attempt(group_id, scope, now, "the reply was not sent")
            bot.activity.log_proactive(group_id, clean, "random")
            record = getattr(bot, "record_outbound", None)
            if not callable(record):
                record = getattr(bot, "record_assistant_turn", None)
            if callable(record):
                record(event, clean, event_id=event.event_id)
            else:
                bot.conversations.add_message(
                    event.event_id, scope, "bot", "Bot", "assistant", clean
                )
            bot.last_reply[scope] = time.time()
            self.store.close_window(scope, group_id, int(time.time()), "posted")
            return Decision(True, "posted", clean)

    # --- bookkeeping ------------------------------------------------------

    def _retry_seconds(
        self, policy: ProactiveConfig, state: dict, last_message_at: int = 0
    ) -> int:
        """Wait longer after each silent decision, capped by freshness.

        A quiet window is a real attempt at conversation, not a retry loop: if
        the bot looked twice and found nothing to say, a third look in the same
        five minutes helps nobody.

        The streak only counts inside the stretch it was earned in. If the group
        has spoken since the last attempt the slate is clean — the bot has not
        been uninspired in *this* conversation yet, and carrying a stale streak
        forward would make it slow to join a group that just came back to life.
        """
        streak = max(0, int(state.get("silent_streak", 0)))
        if int(state.get("last_attempt_at", 0)) < last_message_at:
            streak = 0
        return min(policy.retry_minutes * (1 + streak), policy.freshness_minutes) * 60

    def _not_now(self, group_id: str, scope: str, now: int, reason: str) -> Decision:
        """A gate that closed before the window was even considered."""
        self.store.note_observed(group_id, scope, now, reason)
        return Decision(False, reason)

    def _attempt(self, group_id: str, scope: str, now: int, reason: str) -> Decision:
        """An evaluated turn that produced no post. The window remembers it."""
        self.store.note_attempt(scope, group_id, now, reason)
        return Decision(False, reason)

    # --- in-process trigger ----------------------------------------------

    async def loop(self, connected) -> None:
        """Tick the engine while the gateway is up.

        This is an alternative to the scheduler ``every`` job for deployments
        that would rather not write a schedule entry. It owns no schedule
        semantics — no next-run time, no misfire handling, no run history —
        because those live in ``qunbot/scheduling``. Both trigger paths read and
        write the same persisted window, so running both cannot double-post.
        """
        interval = max(30, self.config.check_seconds)
        while True:
            await asyncio.sleep(interval)
            if not connected():
                continue
            for group_id in sorted(self.bot.policy.allowed_groups):
                try:
                    await self.maybe_post(group_id)
                except Exception:
                    log.exception("Proactive check failed for group %s", group_id)
