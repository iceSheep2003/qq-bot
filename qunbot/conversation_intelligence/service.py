from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from ..domain import MessageEvent
from ..ports import TopicObservationRepository
from .models import ConversationFrame, MessageView, ReplyTarget, TopicView, ToneSignal
from .normalizer import Features, analyze, jaccard


@dataclass
class _Topic:
    topic_id: str
    messages: deque[MessageView] = field(default_factory=lambda: deque(maxlen=40))
    tokens: set[str] = field(default_factory=set)
    entities: set[str] = field(default_factory=set)
    participants: set[str] = field(default_factory=set)
    last_active_at: int = 0


class ConversationIntelligence:
    """Bounded online topic tracker; contains no network or model calls."""

    def __init__(
        self, *, max_messages: int = 200, max_topics: int = 12,
        window_messages: int = 40, window_seconds: int = 300,
        window_char_budget: int = 4800,
        observations: TopicObservationRepository | None = None,
    ):
        self._messages: dict[str, deque[MessageView]] = defaultdict(lambda: deque(maxlen=max_messages))
        self._features: dict[str, Features] = {}
        self._topics: dict[str, list[_Topic]] = defaultdict(list)
        self._message_topic: dict[tuple[str, str], str] = {}
        self.max_topics = max_topics
        self.window_messages = max(4, window_messages)
        self.window_seconds = max(30, window_seconds)
        self.window_char_budget = max(400, window_char_budget)
        self.observations = observations
        self._loaded: set[str] = set()
        self._sequence = 0

    def _view(self, event: MessageEvent) -> MessageView:
        self._sequence += 1
        return MessageView(
            event.event_id, event.platform_message_id or event.event_id,
            event.user_id, event.nickname, event.text or "[图片]",
            event.timestamp or int(time.time()), event.reply_to_message_id,
            event.at_users,
            self._sequence, event.origin,
        )

    def observe(self, event: MessageEvent) -> None:
        if event.origin == "operator":
            return
        self._ensure_loaded(event.scope)
        self._observe(event, persist=True)

    def _ensure_loaded(self, scope: str) -> None:
        if scope in self._loaded:
            return
        self._loaded.add(scope)
        if self.observations is not None:
            for event in self.observations.recent(scope):
                self._observe(event, persist=False)

    def _observe(self, event: MessageEvent, *, persist: bool) -> None:
        key = (event.scope, event.event_id)
        if any(message.event_id == event.event_id for message in self._messages[event.scope]):
            return
        if persist and self.observations is not None:
            self.observations.add(event)
        view = self._view(event)
        features = analyze(view.text)
        self._features[event.event_id] = features
        topics = self._topics[event.scope]
        chosen = self._quoted_topic(event, topics)
        if chosen is None:
            scored = sorted(
                ((self._score(view, features, topic), topic) for topic in topics),
                key=lambda item: (-item[0], item[1].topic_id),
            )
            chosen = scored[0][1] if scored and scored[0][0] >= 0.46 else None
        # Short group-chat continuations often share no words with the turn
        # before them. Preserve adjacency briefly instead of manufacturing a
        # one-message topic. Explicit reply/mention evidence above still wins.
        if chosen is None and topics:
            latest = max(topics, key=lambda item: item.last_active_at)
            gap = max(0, view.timestamp - latest.last_active_at)
            compact = "".join(view.text.split())
            cues = ("这", "那", "然后", "也是", "确实", "看成", "笑死", "所以", "他", "她", "它")
            if gap <= 20 and (len(compact) <= 5 or compact.startswith(cues)):
                chosen = latest
        if chosen is None:
            digest = hashlib.sha1(f"{event.scope}:{event.event_id}".encode()).hexdigest()[:10]
            chosen = _Topic(f"topic-{digest}")
            topics.append(chosen)
        chosen.messages.append(view)
        chosen.tokens.update(features.tokens)
        chosen.entities.update(features.entities)
        chosen.participants.add(view.user_id)
        chosen.last_active_at = view.timestamp
        self._messages[event.scope].append(view)
        self._message_topic[key] = chosen.topic_id
        topics.sort(key=lambda topic: (-topic.last_active_at, topic.topic_id))
        del topics[self.max_topics:]

    def _quoted_topic(self, event: MessageEvent, topics: list[_Topic]) -> _Topic | None:
        if not event.reply_to_message_id:
            return None
        for topic in topics:
            if any(m.platform_message_id == event.reply_to_message_id for m in topic.messages):
                return topic
        return None

    @staticmethod
    def _score(view: MessageView, features: Features, topic: _Topic) -> float:
        if not topic.messages:
            return 0.0
        mentioned = bool(set(view.at_users) & topic.participants)
        entity = jaccard(features.entities, frozenset(topic.entities))
        lexical = jaccard(features.tokens, frozenset(topic.tokens))
        participant = view.user_id in topic.participants
        gap = max(0, view.timestamp - topic.last_active_at)
        decay = max(0.0, 1.0 - gap / 1800)
        return .70 * mentioned + .55 * entity + .45 * lexical + .20 * participant + .15 * decay + .10

    def frame(self, event: MessageEvent) -> ConversationFrame:
        self.observe(event)
        topic_id = self._message_topic[(event.scope, event.event_id)]
        topics = self._topics[event.scope]
        topic = next(item for item in topics if item.topic_id == topic_id)
        focus = next(m for m in topic.messages if m.event_id == event.event_id)
        required: list[MessageView] = [focus]
        quote_id = event.reply_to_message_id
        depth = 0
        while quote_id and depth < 4:
            quoted = next((m for m in self._messages[event.scope] if m.platform_message_id == quote_id), None)
            if quoted is None or quoted in required:
                break
            required.append(quoted)
            quote_id = quoted.reply_to_message_id
            depth += 1
        window = self._recent_window(event.scope, focus, required)
        window_ids = {m.event_id for m in window}
        ambient = [m for m in self._messages[event.scope] if m.event_id not in window_ids][-4:]
        tones = [self._features[m.event_id].tone for m in window if m.event_id in self._features]
        room = ToneSignal(
            polarity=sum(t.polarity for t in tones) / max(1, len(tones)),
            intensity=max((t.intensity for t in tones), default=0.0),
            question=any(t.question for t in tones), joking=any(t.joking for t in tones),
            hostile=any(t.hostile for t in tones),
        )
        quoted = next((m for m in required if m is not focus), None)
        uncertain = not quoted and not event.at_bot and len(topic.messages) == 1
        reasons = (
            ("explicit_reply",) if quoted else
            (("addressed_to_bot",) if event.at_bot else
             (("uncertain_new_topic",) if uncertain else ("active_topic",)))
        )
        target = ReplyTarget(
            focus.platform_message_id, quoted.user_id if quoted else event.user_id,
            event.reply_to_message_id, topic_id,
            1.0 if quoted else (.9 if event.at_bot else (.35 if uncertain else .72)), reasons,
        )
        title = " / ".join(sorted(topic.entities)[:3]) or " ".join(sorted(topic.tokens)[:4])
        frame_hash = hashlib.sha1(f"{event.scope}:{event.event_id}:{topic_id}".encode()).hexdigest()[:12]
        return ConversationFrame(
            frame_hash, event.scope, focus, target, window,
            TopicView(topic_id, title, tuple(topic.messages)[-12:]), tuple(ambient), room,
        )

    def _recent_window(
        self, scope: str, focus: MessageView,
        required: list[MessageView] | tuple[MessageView, ...] = (),
    ) -> tuple[MessageView, ...]:
        """Build a contiguous short-term scene independent of topic labels."""
        cutoff = focus.timestamp - self.window_seconds
        candidates = [
            message for message in self._messages[scope]
            if message.sequence <= focus.sequence and message.timestamp >= cutoff
        ][-self.window_messages:]
        unique = {message.event_id: message for message in (*candidates, *required, focus)}
        ordered = sorted(unique.values(), key=lambda message: message.sequence)
        kept: list[MessageView] = []
        used = 0
        required_ids = {message.event_id for message in required} | {focus.event_id}
        for message in reversed(ordered):
            size = len(message.text)
            if message.event_id not in required_ids and used + size > self.window_char_budget:
                continue
            kept.append(message)
            used += size
        kept.reverse()
        return tuple(kept)

    def latest_frame(self, scope: str) -> ConversationFrame | None:
        """Read the latest real room topic without observing a control event."""
        self._ensure_loaded(scope)
        messages = self._messages.get(scope)
        if not messages:
            return None
        focus = messages[-1]
        topic_id = self._message_topic.get((scope, focus.event_id))
        topic = next((item for item in self._topics[scope] if item.topic_id == topic_id), None)
        if topic is None:
            return None
        window = self._recent_window(scope, focus)
        window_ids = {m.event_id for m in window}
        ambient = tuple(m for m in messages if m.event_id not in window_ids)[-4:]
        tones = [self._features[m.event_id].tone for m in window if m.event_id in self._features]
        room = ToneSignal(
            polarity=sum(t.polarity for t in tones) / max(1, len(tones)),
            intensity=max((t.intensity for t in tones), default=0.0),
            question=any(t.question for t in tones), joking=any(t.joking for t in tones),
            hostile=any(t.hostile for t in tones),
        )
        title = " / ".join(sorted(topic.entities)[:3]) or " ".join(sorted(topic.tokens)[:4])
        return ConversationFrame(
            hashlib.sha1(f"{scope}:latest:{topic_id}".encode()).hexdigest()[:12],
            scope, focus,
            ReplyTarget(focus.platform_message_id, focus.user_id, "", topic_id, .6, ("latest_room_topic",)),
            window, TopicView(topic_id, title, tuple(topic.messages)[-12:]), ambient, room,
        )
