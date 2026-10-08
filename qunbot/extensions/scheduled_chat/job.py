"""Two ways for the bot to say something on its own initiative.

They are deliberately separate actions rather than one handler with a flag,
because they answer different questions:

``chat``          "it is 12:30, the deployer said to water the group" — a fixed
                  time the deployer chose. It lands, or it is skipped for a
                  reason the deployer can read. Never gated by the bot's mood.
``continuation``  "the group has been quiet for a while, is there anything to
                  add?" — the AIReplay-style workflow. Triggered by an ``every``
                  job (the scheduler still owns the clock), gated by the
                  persisted quiet window, and gated by mood, because nobody
                  asked for this one specifically.

Both are ordinary ``JobHandler``s: the scheduler owns next-run times, misfire
handling and the ``skipped``/``failed``/``succeeded`` accounting, so neither
action carries a private timer.
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from ...domain import JobSkipped
from ...replies import ReplyDraft
from ...scheduling import guards
from ...scheduling.registry import JobRuntime, JobSuggestion, suggestion
from .engine import DEFAULT_PROMPT, ProactiveChat, mood_gate
from .policy import GroupPolicySet, ProactiveConfig


class ChatJobHandler:
    """The fixed-time water-the-group action (``chat``)."""

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
        group_id = job["group_id"]
        scope = f"group:{group_id}"
        # A configured cron job is an operator command, not an opportunistic
        # water-the-group attempt.  It runs at its time regardless of whether
        # the room is active or the bot spoke recently.
        event = bot.job_event(job, int(time.time()))
        async with bot.scope_lock(scope):
            reply = await bot.agent.reply(event, proactive=True)
            # Preserve the structured channel decision.  Flattening this to
            # reply.text silently turned scheduled voice/meme replies into
            # text-only messages.
            draft = getattr(reply, "draft", None)
            if draft is not None and callable(getattr(bot, "send_draft", None)):
                clean = await bot.send_draft(group_id, None, draft, event=event)
            else:
                clean = await bot.send_reply(group_id, None, reply.text.strip())
            if not clean:
                raise JobSkipped("scheduled model produced no output")
            guards.record(bot, job, event, clean, "cron")


class DeliveryJobHandler:
    """Deliver an operator-authored media draft without asking the model.

    This is the deterministic cron primitive for announcements and deployment
    probes.  It is only reachable from trusted schedule configuration/database
    rows; chat users have no command surface that can create one.
    """

    action = "deliver"

    def suggested_jobs(self) -> list[JobSuggestion]:
        return []

    @staticmethod
    def validate_payload(payload: Mapping) -> None:
        text = payload.get("text")
        channels = payload.get("channels", ["text"])
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise ValueError("deliver payload.text must contain 1-300 characters")
        if (
            not isinstance(channels, list)
            or not channels
            or any(item not in {"text", "voice", "meme"} for item in channels)
        ):
            raise ValueError("deliver payload.channels must contain text, voice and/or meme")
        if "meme" in channels and not str(payload.get("meme_tag") or "").strip():
            raise ValueError("deliver payload.meme_tag is required for meme")

    async def run(self, bot: JobRuntime, job: dict) -> None:
        group_id = job["group_id"]
        scope = f"group:{group_id}"
        payload = dict(bot.job_spec(job).payload)
        self.validate_payload(payload)
        text = payload["text"].strip()
        channels = tuple(dict.fromkeys(payload.get("channels", ["text"])))
        event = bot.job_event(job, int(time.time()))
        draft = ReplyDraft(
            text=text,
            channels=channels,
            voice_text=str(payload.get("voice_text") or text).strip()[:300],
            meme_tag=str(payload.get("meme_tag") or "").strip()[:40],
            intent="scheduled_delivery",
        )
        async with bot.scope_lock(scope):
            clean = await bot.send_draft(group_id, None, draft, event=event)
            if not clean:
                raise JobSkipped("media delivery produced no output")
            guards.record(bot, job, event, clean, "cron")


class ContinuationJobHandler:
    """The post-message interval action (``continuation``).

    The schedule is ``every`` N seconds; what that tick *means* is decided by
    the persisted quiet window, not by the clock. A skipped run carries the
    reason into ``job_runs.detail`` so a deployer can see why the bot stayed
    quiet ("group is still talking", "not in the mood to start a conversation",
    "the model chose to stay silent").
    """

    action = "continuation"

    def __init__(
        self, config: ProactiveConfig | None = None, policies: GroupPolicySet | None = None
    ):
        self.config = config if config is not None else ProactiveConfig.from_env()
        self.policies = (
            policies
            if policies is not None
            else GroupPolicySet.load(self.config.groups_path)
        )

    def suggested_jobs(self) -> list[JobSuggestion]:
        return [
            suggestion(
                "continue-quiet",
                str(self.config.check_seconds),
                DEFAULT_PROMPT,
                kind="every",
                description="消息后间隔续聊：群里安静下来后判断是否接一句",
            )
        ]

    async def run(self, bot: JobRuntime, job: dict) -> None:
        engine = ProactiveChat(bot, self.config, mood_gate(bot), self.policies)
        prompt = (job.get("prompt") or "").strip() or None
        decision = await engine.maybe_post(job["group_id"], prompt=prompt)
        if not decision.posted:
            # An expected no-op, not a failure: the scheduler records it as
            # ``skipped`` with this reason rather than logging an error.
            raise JobSkipped(decision.reason)


def register_jobs(registry, _config) -> None:
    """Register both actions. ``_config`` is the core Config, deliberately unused.

    Each handler reads its own environment and its own local files; the core
    Config must not grow feature fields.
    """
    registry.register(ChatJobHandler())
    registry.register(DeliveryJobHandler())
    registry.register(ContinuationJobHandler())
