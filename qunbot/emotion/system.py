"""The single object the application wires up.

Everything the core needs from this package goes through four methods here;
``ports.MoodObserver`` describes the two the service uses, and the context
provider uses ``narration``.

The clock is a constructor argument (defaulting to the wall clock) so decay,
assessment and replay all run on a fixed clock in tests. Nothing here reads
the time by itself, which is what makes a 2000-event simulation reproducible.
"""

from __future__ import annotations

import logging
import time

from ..domain import MessageEvent
from ..ports import ChatModel
from .config import EmotionConfig
from .evaluator import MoodEvaluator
from .state import EmotionPolicy, Mood, MoodSnapshot, replay, timeline
from .store import EmotionStore

log = logging.getLogger(__name__)


class EmotionSystem:
    def __init__(self, config: EmotionConfig, model: ChatModel, *, clock=time.time):
        self.config = config
        self.clock = clock
        self.policy = EmotionPolicy(
            half_life_minutes=config.decay_minutes,
            sensitivity=config.sensitivity,
            min_sociability=config.min_sociability,
            coupling=config.coupling,
        )
        self.store = EmotionStore(config.db_path)
        self.evaluator = MoodEvaluator(
            model,
            self.store,
            self.policy,
            auto_enabled=config.auto_enabled,
            clock=clock,
        )

    def current(self, scope: str) -> Mood:
        """The mood as of now — stored value relaxed toward baseline."""
        return self.policy.decay(Mood.from_row(self.store.load(scope)), int(self.clock()))

    def narration(self, scope: str) -> str:
        return self.policy.narrate(self.current(scope))

    def permits_proactive(self, scope: str) -> bool:
        return self.policy.permits_proactive(self.current(scope))

    async def observe(self, event: MessageEvent, bot_reply: str) -> None:
        await self.evaluator.observe(event, bot_reply)

    def history(self, scope: str) -> tuple[MoodSnapshot, ...]:
        """Every mood the log accounts for, oldest first.

        Rebuilt from ``mood_events`` alone, so it answers "what was the mood
        right after each change?" without storing a snapshot per turn.
        """
        return timeline(self.policy, self.store.events(scope))

    def replay(self, scope: str, *, upto: int | None = None) -> Mood:
        """Rebuild the mood from the log, decayed forward to ``upto``."""
        return replay(self.policy, self.store.events(scope), upto=upto)

    def mood_at(self, scope: str, when: int) -> Mood:
        """What the mood was at ``when`` — the replay question, spelled out."""
        return self.replay(scope, upto=int(when))

    def close(self) -> None:
        self.store.close()


def build_emotion(
    config: EmotionConfig, model: ChatModel, *, clock=time.time
) -> EmotionSystem:
    """Composition helper for app.py. Assumes config.enabled is already true."""
    return EmotionSystem(config, model, clock=clock)
