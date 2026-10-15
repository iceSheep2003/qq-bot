"""Application use cases. No NapCat, HTTP or SQLite imports.

The conversation lifecycle is split into four explicit use cases so each can be
tested and replaced on its own:

``ingest``            filter, deduplicate and record an inbound message
``decide_reply``      the reply policy (today: group messages only when @-ed)
``execute_turn``      take the scope lock, call the model, send and record
``dispatch_post_reply`` hand best-effort observations to a bounded queue

Post-reply work (affection, mood, other observers) is delegated to
``agent.observation.ObservationDispatcher``. It owns bounded in-memory and
optional durable queues, idempotent event keys, recovery and shutdown so the
interactive service does not carry a second maintenance lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from qqbot.contracts import models
from qqbot.contracts.models import MessageEvent
from qqbot.agent.conversation.channels import ChannelPolicy
from qqbot.agent.conversation.delivery import ConversationDelivery
from qqbot.contracts.ports import (
    ActivityRepository,
    AffectionObserver,
    AgentRunner,
    InboundMediaPort,
    ObservationRepository,
    ConversationRepository,
    ConversationIntelligencePort,
    MediaProcessor,
    MessageReactionPolicy,
    MessageReactionSender,
    LightInteractionPolicyPort,
    NativeInteractionSender,
    MessageSender,
    PeopleRepository,
    PromptSessionRepository,
    ReplyDecisionPolicy,
    ReplyDelivery,
    ReplyObserver,
    ReplyPlanner,
    TurnGuard,
)
from qqbot.actions.replies import (
    DefaultReplyPlanner,
    ReplyDispatcher,
    ReplyDraft,
    parse_reply_draft,
)
from qqbot.actions.replies.repetition import repeats_recent
from qqbot.agent.conversation.maintenance import ConversationMaintenance
from qqbot.agent.observation import ObservationDispatcher

log = logging.getLogger(__name__)

# Bounds on the post-reply observation queue. Overridable per instance; the
# environment-facing names are parsed in config.Config.
DEFAULT_OBSERVATION_BACKLOG = 256
DEFAULT_OBSERVATION_WORKERS = 1
DEFAULT_OBSERVATION_DEDUPE = 4096


class ConversationService:
    def __init__(
        self,
        agent: AgentRunner,
        conversations: ConversationRepository,
        people: PeopleRepository,
        activity: ActivityRepository,
        sender: MessageSender,
        policy: models.BotPolicy,
        affection: AffectionObserver | None = None,
        media: MediaProcessor | None = None,
        observers: tuple[ReplyObserver, ...] = (),
        *,
        reply_planner: ReplyPlanner | None = None,
        reply_dispatcher: ReplyDelivery | None = None,
        conversation_intelligence: ConversationIntelligencePort | None = None,
        reply_policy: ReplyDecisionPolicy | None = None,
        turn_guard: TurnGuard | None = None,
        reaction_policy: MessageReactionPolicy | None = None,
        reaction_sender: MessageReactionSender | None = None,
        light_interaction_policy: LightInteractionPolicyPort | None = None,
        native_interaction_sender: NativeInteractionSender | None = None,
        prompt_sessions: PromptSessionRepository | None = None,
        observation_backlog: int | None = None,
        observation_workers: int | None = None,
        observation_dedupe: int | None = None,
        observation_store: ObservationRepository | None = None,
        inbound_media: InboundMediaPort | None = None,
        restore_last_reply: bool = True,
    ):
        self.agent, self.conversations, self.activity = agent, conversations, activity
        self.people = people
        self.sender, self.policy = sender, policy
        self.media = media
        self.inbound_media = inbound_media
        self.reply_planner = reply_planner or DefaultReplyPlanner()
        self.reply_dispatcher = reply_dispatcher or ReplyDispatcher(sender, media)
        self.delivery = ConversationDelivery(
            conversations, self.reply_dispatcher, prompt_sessions=prompt_sessions,
            conversation_intelligence=conversation_intelligence, logger=log,
        )
        self.conversation_intelligence = conversation_intelligence
        # None means the built-in "@ me only" rule; an injected policy replaces
        # it. Advisory only — a failure falls back to the default.
        self.reply_policy = reply_policy
        self.turn_guard = turn_guard
        self.reaction_policy = reaction_policy
        self.reaction_sender = reaction_sender
        self.light_interaction_policy = light_interaction_policy
        self.native_interaction_sender = native_interaction_sender
        self.prompt_sessions = prompt_sessions
        self.scope_locks: dict[str, asyncio.Lock] = {}
        self.last_reply: dict[str, float] = {}
        # Bounded, at-most-once post-reply observation queue. Created lazily so
        # a service that never replies never allocates a worker.
        # Defaults live here; environment parsing lives in config.Config, which
        # app.py passes down. This module never reads os.environ.
        self.post_reply = ObservationDispatcher(
            affection=affection, observers=tuple(observers),
            affection_enabled=policy.affection_auto_enabled,
            backlog=observation_backlog or DEFAULT_OBSERVATION_BACKLOG,
            workers=observation_workers or DEFAULT_OBSERVATION_WORKERS,
            dedupe=observation_dedupe or DEFAULT_OBSERVATION_DEDUPE,
            store=observation_store, logger=log,
        )
        self.maintenance = ConversationMaintenance(
            message_count=lambda scope: conversations.message_count(scope),
            extract_memory=lambda scope: agent.extract_memory(scope),
            extract_every=policy.memory_extract_every,
            compactor=getattr(agent, "conversation_compactor", None),
            logger=log,
        )
        self.observations = self.post_reply.stats
        if restore_last_reply:
            self.restore_last_reply()

    # --- clocks ----------------------------------------------------------

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

    # --- restart behaviour -----------------------------------------------

    def restore_last_reply(self) -> None:
        """Rebuild the reply cooldown from persisted history.

        Scope locks and maintenance reservations are in-process coordination and
        are intentionally *not* restored: a lock has no meaning before the
        process starts, and a half-finished extraction simply runs again on the
        next message. ``last_reply`` is different — the scheduler and the
        proactive worker read it to avoid talking over a reply that already
        happened, so losing it would make the bot double-post after a restart.
        It is rebuilt here from the newest assistant turn per allowlisted group.
        """
        for group_id in self.policy.allowed_groups:
            scope = f"group:{group_id}"
            try:
                rows = self.conversations.recent(scope, 24)
            except Exception:
                log.exception("Could not restore last_reply for %s", scope)
                continue
            for row in reversed(rows):
                if row["role"] == "assistant":
                    self.last_reply[scope] = float(row["created_at"])
                    break

    # --- use case 1: ingest ----------------------------------------------

    async def handle_message(self, event: MessageEvent) -> None:
        """Full inbound path: ingest, decide, execute. Entry point for app.py."""
        if not await self.ingest(event):
            return
        if self.inbound_media is not None:
            event = await self.inbound_media.prepare(event)
        if await self.maybe_light_interaction(event):
            return
        await self.maybe_react(event)
        if not await self.decide_reply(event):
            return
        guarded_event = await self.apply_turn_guard(event)
        if guarded_event is None:
            return
        event = guarded_event
        await self.execute_turn(event)

    async def maybe_react(self, event: MessageEvent) -> bool:
        """Best-effort lightweight presence; never controls text admission."""
        if self.reaction_policy is None or self.reaction_sender is None:
            return False
        try:
            decision = self.reaction_policy.decide(event)
            if not getattr(decision, "react", False):
                return False
            await self.reaction_sender.react_to_message(
                event.platform_message_id, str(decision.emoji_id)
            )
            log.info(
                "Reacted event=%s emoji=%s(%s) reason=%s",
                event.event_id,
                decision.name,
                decision.emoji_id,
                decision.reason,
            )
            return True
        except Exception:
            # A decorative platform gesture must never suppress or delay the
            # actual conversation path when NapCat rejects an emoji ID.
            log.exception("Message reaction failed event=%s", event.event_id)
            return False

    async def maybe_light_interaction(self, event: MessageEvent) -> bool:
        """Handle repetition and tiny native games without spending API tokens."""
        if (
            self.light_interaction_policy is None
            or self.native_interaction_sender is None
        ):
            return False
        try:
            decision = self.light_interaction_policy.decide(
                event, self.conversations.recent(event.scope, 8)
            )
            if not getattr(decision, "active", False):
                return False
            if decision.kind == "repeat":
                await self.sender.send(group_id=event.group_id, text=decision.text)
                transcript = decision.text
            elif decision.kind in {"dice", "rps"}:
                await self.native_interaction_sender.send_native(
                    str(event.group_id), decision.kind
                )
                transcript = "[骰子]" if decision.kind == "dice" else "[猜拳]"
            else:
                return False
            self.record_assistant_turn(
                event, transcript, event_id=f"local:{decision.kind}:{event.event_id}"
            )
            self.last_reply[event.scope] = time.time()
            log.info(
                "Local interaction event=%s kind=%s", event.event_id, decision.kind
            )
            return True
        except Exception:
            log.exception("Local interaction failed event=%s", event.event_id)
            return False

    async def apply_turn_guard(self, event: MessageEvent) -> MessageEvent | None:
        """Attach a trusted social cue or suppress repeated bait."""
        if self.turn_guard is None:
            return event
        try:
            decision = await self.turn_guard.decide(
                event, recent=self.conversations.recent(event.scope, 16)
            )
        except Exception:
            log.exception("Turn guard failed; continuing without a social cue")
            return event
        action = getattr(decision, "action", "ignore")
        if action != "allow":
            log.info(
                "Turn ignored by identity guard: %s", getattr(decision, "reason", "")
            )
            return None
        cue = str(getattr(decision, "category", ""))
        return replace(event, social_cue=cue) if cue else event

    async def ingest(self, event: MessageEvent) -> bool:
        """Filter, deduplicate and record. Returns whether it was stored.

        A message is stored exactly once: the message table's unique ``event_id``
        makes a redelivered frame (reconnect, OneBot retry) a no-op here, so it
        never reaches the reply decision or the observation queue again.
        """
        channels = ChannelPolicy(self.policy.allowed_groups, self.policy.private_enabled)
        if not channels.accepts(event):
            return False
        text = event.text or (
            "[非文字消息]" if event.image_urls or event.inbound_parts else ""
        )
        if not text:
            return False
        add_event = getattr(self.conversations, "add_event", None)
        inserted = (
            add_event(event)
            if callable(add_event)
            else self.conversations.add_message(
                event.event_id, event.scope, event.user_id, event.nickname, "user", text
            )
        )
        if not inserted:
            return False
        self.people.observe_user(event.user_id, event.nickname)
        if event.group_id:
            # The per-group fact. A person can set a different card in each
            # group, so it is recorded against the group rather than promoted
            # to the global nickname.
            self.people.observe_group_member(
                event.group_id, event.user_id, event.nickname, event.card
            )
        if self.conversation_intelligence is not None:
            self.conversation_intelligence.observe(event)
        self.maintenance.maybe_extract(event.scope)
        return True

    # --- use case 2: reply decision --------------------------------------

    async def decide_reply(self, event: MessageEvent) -> bool:
        """The reply policy. Default: a group message must @ the bot.

        One small decision, so a replaceable policy (read-the-room, abstain)
        can be injected without touching turn execution. An injected policy is
        advisory — a disabled feature simply means the default applies.
        """
        if not event.group_id:
            return ChannelPolicy(self.policy.allowed_groups, self.policy.private_enabled).accepts(event)
        if self.reply_policy is None:
            return not event.group_id or event.at_bot
        try:
            decision = bool(
                await self.reply_policy.decide(
                    event, recent=self.conversations.recent(event.scope, 12)
                )
            )
            detail = getattr(getattr(self.reply_policy, "last", None), "reason", "")
            log.info(
                "Reply admission event=%s scope=%s allowed=%s reason=%s",
                event.event_id,
                event.scope,
                decision,
                detail,
            )
            return decision
        except Exception:
            # An installed admission policy is also the API-spend boundary.
            # Failing open here would let a broken quota/decay extension turn
            # every mention into an unbounded model call.
            log.exception("Reply decision policy failed; refusing the model call")
            return False

    # --- use case 3: turn execution --------------------------------------

    async def execute_turn(self, event: MessageEvent) -> str | None:
        """Take the scope lock, call the model, send and record the reply."""
        async with self.scope_lock(event.scope):
            try:
                reply = await self.agent.reply(event)
            except Exception:
                log.exception("Model call failed")
                return None
            draft = getattr(reply, "draft", None) or parse_reply_draft(reply.text)
            if not draft.text and not draft.channels:
                return None
            planning_event = event
            if self.conversation_intelligence is not None and event.group_id:
                frame = self.conversation_intelligence.latest_frame(event.scope)
                if frame is not None:
                    ids = tuple(
                        item.platform_message_id
                        for item in frame.recent_window
                        if item.platform_message_id
                    )
                    planning_event = replace(event, recent_message_ids=ids)
            plan = await self.reply_planner.plan(draft, planning_event)
            # A model can still imitate the room even when the lightweight
            # repeat policy did not fire. Treat exact echoes as repeats at the
            # final model boundary: only an explicit ``+1`` event may produce
            # one. This protects both normal read-the-room replies and
            # proactive turns from accidentally parroting a member.
            if event.group_id and self._is_unrequested_echo(event, plan.semantic_text):
                log.info("Suppressing unrequested model echo event=%s", event.event_id)
                return None
            delivered = await self.delivery.deliver(
                plan,
                group_id=event.group_id,
                user_id=None if event.group_id else event.user_id,
                allowed_at=self._roster(event),
                event=event,
            )
            if not delivered.sent:
                return None
            clean = delivered.transcript
            self.delivery.record_transcript(event, clean)
            commit = getattr(self.agent, "commit_delivered_reply", None)
            if callable(commit):
                commit(reply, event, clean)
            self.delivery.observe_assistant(event, clean, f"reply:{event.event_id}")
            self.last_reply[event.scope] = time.time()
            record = getattr(self.reply_policy, "record_delivery", None)
            if callable(record):
                record(event, clean)
            self.dispatch_post_reply(event, clean, draft)
            self.maintenance.after_reply(event)
            return clean

    def _is_unrequested_echo(self, event: MessageEvent, text: str) -> bool:
        if self._normalize_repeat_marker(event.text) == "+1":
            return False
        try:
            recent = self.conversations.recent(event.scope, 32)
        except Exception:
            return False
        return repeats_recent(text, recent)

    @staticmethod
    def _normalize_repeat_marker(text: str) -> str:
        return re.sub(r"\s+", "", str(text or "")).lower()

    def record_assistant_turn(self, trigger: MessageEvent, text: str, *, event_id: str) -> None:
        self.delivery.record_assistant(trigger, text, event_id=event_id)

    def record_outbound(self, trigger, text, *, event_id):
        """Scheduling port uses the same actual-delivery recording boundary."""
        self.record_assistant_turn(trigger, text, event_id=event_id)

    async def send_reply(
        self,
        group_id: str | None,
        user_id: str | None,
        text: str,
        event: MessageEvent | None = None,
    ) -> str:
        """Compatibility entry point for jobs that still produce plain text.

        ``event`` supplies the roster an ``[[at:...]]`` marker is allowed to
        reference. Without it no @ target is granted, so a caller that cannot
        prove who is in the group never lets the model address anyone.
        """
        return await self.send_draft(
            group_id, user_id, parse_reply_draft(text), event=event
        )

    async def send_draft(
        self,
        group_id: str | None,
        user_id: str | None,
        draft: ReplyDraft,
        event: MessageEvent | None = None,
    ) -> str:
        """Deliver a structured model reply through the shared media pipeline.

        Scheduled and proactive turns must retain the model's channel decision
        (text/meme/voice) instead of flattening it back to plain text.  This is
        the same delivery boundary used by ordinary inbound turns.
        """
        plan = await self.reply_planner.plan(draft, event)
        result = await self.delivery.deliver(
            plan,
            group_id=group_id,
            user_id=user_id,
            allowed_at=self._roster(event) if event else frozenset(),
            event=event,
        )
        return result.transcript if result.sent else ""

    @staticmethod
    def _roster(event: MessageEvent | None) -> frozenset[str]:
        """Members the platform asserted are present, as @-target evidence.

        The trigger's sender and anyone @-ed in the triggering message. Both are
        facts OneBot reported, never strings the model produced — which is the
        whole point of validating an @ target against them.
        """
        if event is None:
            return frozenset()
        return frozenset(
            str(value) for value in (event.user_id, *event.at_users) if value
        )

    # --- use case 4: post-reply events -----------------------------------

    def dispatch_post_reply(self, event: MessageEvent, bot_reply: str, draft: object | None = None) -> None:
        """Delegate advisory work to the single post-reply owner."""
        self.post_reply.dispatch(event, bot_reply, draft)

    def submit_observation(self, key: str, observer: ReplyObserver, event: MessageEvent, bot_reply: str) -> bool:
        return self.post_reply.submit(key, observer, event, bot_reply)

    @property
    def pending_observations(self) -> int:
        return self.post_reply.pending

    async def durable_observation_loop(self):
        return await self.post_reply.run_durable()

    async def aclose(self, timeout: float = 5.0) -> None:
        """Settle maintenance even when observation draining fails or is cancelled."""
        maintenance_close = asyncio.create_task(self.maintenance.aclose())
        try:
            await self.post_reply.aclose(timeout=timeout)
        finally:
            await maintenance_close

    def scope_lock(self, scope: str) -> asyncio.Lock:
        return self.scope_locks.setdefault(scope, asyncio.Lock())
