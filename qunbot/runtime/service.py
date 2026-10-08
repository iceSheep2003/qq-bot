"""Application use cases. No NapCat, HTTP or SQLite imports.

The conversation lifecycle is split into four explicit use cases so each can be
tested and replaced on its own:

``ingest``            filter, deduplicate and record an inbound message
``decide_reply``      the reply policy (today: group messages only when @-ed)
``execute_turn``      take the scope lock, call the model, send and record
``dispatch_post_reply`` hand best-effort observations to a bounded queue

Post-reply work (affection, mood, other observers) runs on a bounded worker
queue with idempotent event keys instead of one bare ``create_task`` per reply.
The guarantee is **at most once**: a key is admitted only once, a full backlog
drops and counts work rather than duplicating it, and a failed observation is
logged and never retried. Nothing in the queue is persisted, so a restart loses
queued observations — that is a deliberate trade (observations are advisory and
must never double-count) rather than a gap to fix with a database table.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..domain import MessageEvent
from ..ports import (
    ActivityRepository,
    AffectionObserver,
    AgentRunner,
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
from ..replies import DefaultReplyPlanner, ReplyDispatcher, ReplyDraft, parse_reply_draft
from ..replies.repetition import repeats_recent

log = logging.getLogger(__name__)

# Bounds on the post-reply observation queue. Overridable per instance; the
# environment-facing names are parsed in config.Config.
DEFAULT_OBSERVATION_BACKLOG = 256
DEFAULT_OBSERVATION_WORKERS = 1
DEFAULT_OBSERVATION_DEDUPE = 4096


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


@dataclass
class ObservationStats:
    """Counters for the post-reply queue. Observability without a dependency."""

    enqueued: int = 0
    dropped: int = 0
    deduplicated: int = 0
    completed: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _DraftObserver:
    observer: object
    draft: object

    async def observe(self, event: MessageEvent, bot_reply: str) -> None:
        await self.observer.observe_draft(event, bot_reply, self.draft)


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
        restore_last_reply: bool = True,
    ):
        self.agent, self.conversations, self.activity = agent, conversations, activity
        self.people = people
        self.sender, self.policy = sender, policy
        self.affection = affection
        self.media = media
        self.reply_planner = reply_planner or DefaultReplyPlanner()
        self.reply_dispatcher = reply_dispatcher or ReplyDispatcher(sender, media)
        self.conversation_intelligence = conversation_intelligence
        self.observers = observers
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
        self.extracting: set[str] = set()
        self.extract_slots = asyncio.Semaphore(2)
        # Bounded, at-most-once post-reply observation queue. Created lazily so
        # a service that never replies never allocates a worker.
        # Defaults live here; environment parsing lives in config.Config, which
        # app.py passes down. This module never reads os.environ.
        self._observation_backlog = observation_backlog or DEFAULT_OBSERVATION_BACKLOG
        self._observation_workers = observation_workers or DEFAULT_OBSERVATION_WORKERS
        self._dedupe_limit = observation_dedupe or DEFAULT_OBSERVATION_DEDUPE
        self._queue: asyncio.Queue | None = None
        self._workers: list[asyncio.Task] = []
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._extract_tasks: set[asyncio.Task] = set()
        self._closed = False
        self.observations = ObservationStats()
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

        ``scope_locks`` and ``extracting`` are pure in-process coordination and
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
                event.event_id, decision.name, decision.emoji_id, decision.reason,
            )
            return True
        except Exception:
            # A decorative platform gesture must never suppress or delay the
            # actual conversation path when NapCat rejects an emoji ID.
            log.exception("Message reaction failed event=%s", event.event_id)
            return False

    async def maybe_light_interaction(self, event: MessageEvent) -> bool:
        """Handle repetition and tiny native games without spending API tokens."""
        if self.light_interaction_policy is None or self.native_interaction_sender is None:
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
            log.info("Local interaction event=%s kind=%s", event.event_id, decision.kind)
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
            log.info("Turn ignored by identity guard: %s", getattr(decision, "reason", ""))
            return None
        cue = str(getattr(decision, "category", ""))
        return replace(event, social_cue=cue) if cue else event

    async def ingest(self, event: MessageEvent) -> bool:
        """Filter, deduplicate and record. Returns whether it was stored.

        A message is stored exactly once: the message table's unique ``event_id``
        makes a redelivered frame (reconnect, OneBot retry) a no-op here, so it
        never reaches the reply decision or the observation queue again.
        """
        if not event.group_id and not self.policy.private_enabled:
            return False
        if event.group_id and event.group_id not in self.policy.allowed_groups:
            return False
        text = event.text or ("[图片]" if event.image_urls else "")
        if not text:
            return False
        if not self.conversations.add_message(
            event.event_id, event.scope, event.user_id, event.nickname, "user", text
        ):
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
        self._maybe_extract(event.scope)
        return True

    # --- use case 2: reply decision --------------------------------------

    async def decide_reply(self, event: MessageEvent) -> bool:
        """The reply policy. Default: a group message must @ the bot.

        One small decision, so a replaceable policy (read-the-room, abstain)
        can be injected without touching turn execution. An injected policy is
        advisory — a disabled feature simply means the default applies.
        """
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
                event.event_id, event.scope, decision, detail,
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
            delivered = await self.reply_dispatcher.dispatch(
                plan,
                group_id=event.group_id,
                user_id=None if event.group_id else event.user_id,
                allowed_at=self._roster(event),
            )
            if not delivered.sent:
                return None
            clean = delivered.transcript
            self.conversations.add_message(
                f"reply:{event.event_id}",
                event.scope,
                "bot",
                "Bot",
                "assistant",
                clean,
            )
            commit = getattr(self.agent, "commit_delivered_reply", None)
            if callable(commit):
                commit(reply, event, clean)
            self._observe_assistant(event, clean, f"reply:{event.event_id}")
            self.last_reply[event.scope] = time.time()
            record = getattr(self.reply_policy, "record_delivery", None)
            if callable(record):
                record(event, clean)
            self.dispatch_post_reply(event, clean, draft)
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

    def record_assistant_turn(
        self, trigger: MessageEvent, text: str, *, event_id: str
    ) -> None:
        """Persist and index one actual outbound turn through one boundary."""
        self.conversations.add_message(
            event_id, trigger.scope, "bot", "Bot", "assistant", text
        )
        if self.prompt_sessions is not None and text.strip():
            try:
                self.prompt_sessions.append(
                    trigger.scope, [{"role": "assistant", "content": text.strip()}]
                )
            except Exception:
                log.exception("Could not sync local assistant turn event=%s", event_id)
        self._observe_assistant(trigger, text, event_id)

    def _observe_assistant(
        self, trigger: MessageEvent, text: str, event_id: str
    ) -> None:
        if self.conversation_intelligence is None or not trigger.group_id:
            return
        reply_to = trigger.platform_message_id
        if trigger.origin == "operator":
            latest = self.conversation_intelligence.latest_frame(trigger.scope)
            reply_to = latest.focus.platform_message_id if latest is not None else ""
        self.conversation_intelligence.observe(MessageEvent(
            event_id=event_id,
            scope=trigger.scope,
            group_id=trigger.group_id,
            user_id="bot",
            nickname="Kinna",
            text=text,
            image_urls=(),
            at_bot=False,
            at_users=(),
            timestamp=int(time.time()),
            platform_message_id=event_id,
            reply_to_message_id=reply_to,
            reply_to_user_id=trigger.user_id if trigger.origin != "operator" else "",
            origin="assistant",
        ))

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
        result = await self.reply_dispatcher.dispatch(
            plan,
            group_id=group_id,
            user_id=user_id,
            allowed_at=self._roster(event) if event else frozenset(),
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

    def dispatch_post_reply(
        self, event: MessageEvent, bot_reply: str, draft: object | None = None
    ) -> None:
        """Queue best-effort observations. Never awaited by the caller."""
        if self.affection and self.policy.affection_auto_enabled and event.group_id:
            observer = self._observer_for_draft(self.affection, draft)
            self.submit_observation(
                f"affection:{event.event_id}", observer, event, bot_reply
            )
        for index, observer in enumerate(self.observers):
            observer = self._observer_for_draft(observer, draft)
            self.submit_observation(
                f"observer:{index}:{event.event_id}", observer, event, bot_reply
            )

    @staticmethod
    def _observer_for_draft(observer: object, draft: object | None):
        if (
            draft is not None
            and getattr(draft, "state_observations", None) is not None
            and callable(getattr(observer, "observe_draft", None))
        ):
            return _DraftObserver(observer, draft)
        return observer

    def submit_observation(
        self,
        key: str,
        observer: ReplyObserver,
        event: MessageEvent,
        bot_reply: str,
    ) -> bool:
        """Admit one observation, at most once per key.

        The key is claimed before it is queued: a redelivered event, a failure
        replay or a retry can never observe the same turn twice. If the backlog
        is full the observation is dropped and counted — the alternative
        (unbounded growth, or replaying a partially applied observation) is
        worse than a missing affection nudge.
        """
        if self._closed:
            return False
        if key in self._seen:
            self.observations.deduplicated += 1
            return False
        self._remember(key)
        queue = self._ensure_queue()
        try:
            queue.put_nowait((key, observer, event, bot_reply))
        except asyncio.QueueFull:
            self.observations.dropped += 1
            log.warning("Observation backlog full; dropped %s", key)
            return False
        self.observations.enqueued += 1
        return True

    def _remember(self, key: str) -> None:
        self._seen[key] = None
        while len(self._seen) > self._dedupe_limit:
            self._seen.popitem(last=False)

    def _ensure_queue(self) -> asyncio.Queue:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._observation_backlog)
        if not any(not task.done() for task in self._workers):
            self._workers = [
                asyncio.get_running_loop().create_task(self._observation_worker())
                for _ in range(self._observation_workers)
            ]
        return self._queue

    async def _observation_worker(self) -> None:
        queue = self._queue
        while True:
            item = await queue.get()
            key, observer, event, bot_reply = item
            try:
                await observer.observe(event, bot_reply)
                self.observations.completed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                # At-most-once: a failed observation is logged, never retried,
                # so it cannot be counted twice on a later replay.
                self.observations.failed += 1
                log.exception("Post-reply observation failed for %s", key)
            finally:
                queue.task_done()

    @property
    def pending_observations(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    async def aclose(self, timeout: float = 5.0) -> None:
        """Stop accepting new work, drain the queue, then cancel what remains.

        Rule at shutdown: already-queued observations get ``timeout`` seconds to
        finish; anything still running after that is cancelled, and nothing is
        written to disk to resume later. Callers that do not await ``aclose``
        simply lose the queued observations on exit.
        """
        self._closed = True
        queue = self._queue
        workers = [task for task in self._workers if not task.done()]
        if queue is not None and workers:
            try:
                await asyncio.wait_for(queue.join(), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError):
                log.warning(
                    "Observation queue did not drain within %.1fs; dropping %d",
                    timeout,
                    queue.qsize(),
                )
            except asyncio.CancelledError:
                # Shutdown itself was cancelled: stop the workers without waiting.
                for task in workers:
                    task.cancel()
                raise
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers = []
        tasks = list(self._extract_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            self._extract_tasks.clear()

    # --- memory extraction ------------------------------------------------

    def _maybe_extract(self, scope: str) -> None:
        if (
            self.conversations.message_count(scope) % self.policy.memory_extract_every
            == 0
            and scope not in self.extracting
        ):
            self.extracting.add(scope)
            task = asyncio.get_running_loop().create_task(self._extract_safe(scope))
            self._extract_tasks.add(task)
            task.add_done_callback(self._extract_tasks.discard)

    async def _extract_safe(self, scope: str) -> None:
        try:
            async with self.extract_slots:
                await self.agent.extract_memory(scope)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Memory extraction failed for %s", scope)
        finally:
            self.extracting.discard(scope)

    def scope_lock(self, scope: str) -> asyncio.Lock:
        return self.scope_locks.setdefault(scope, asyncio.Lock())
