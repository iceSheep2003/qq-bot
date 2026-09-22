"""The single object the application wires up.

Everything the core needs from this package goes through four methods here;
``ports.MoodObserver`` describes the two the service uses, and the context
provider uses ``narration``.
"""

from __future__ import annotations

import logging
import time

from ..domain import MessageEvent
from ..ports import ChatModel
from .config import EmotionConfig
from .evaluator import MoodEvaluator
from .state import EmotionPolicy, Mood
from .store import EmotionStore

log = logging.getLogger(__name__)


class EmotionSystem:
    def __init__(self, config: EmotionConfig, model: ChatModel):
        self.config = config
        self.policy = EmotionPolicy(
            half_life_minutes=config.decay_minutes,
            sensitivity=config.sensitivity,
            min_sociability=config.min_sociability,
        )
        self.store = EmotionStore(config.db_path)
        self.evaluator = MoodEvaluator(
            model, self.store, self.policy, auto_enabled=config.auto_enabled
        )

    def current(self, scope: str) -> Mood:
        """The mood as of now — stored value relaxed toward baseline."""
        return self.policy.decay(Mood.from_row(self.store.load(scope)), int(time.time()))

    def narration(self, scope: str) -> str:
        return self.policy.narrate(self.current(scope))

    def permits_proactive(self, scope: str) -> bool:
        return self.policy.permits_proactive(self.current(scope))

    async def observe(self, event: MessageEvent, bot_reply: str) -> None:
        await self.evaluator.observe(event, bot_reply)

    def close(self) -> None:
        self.store.close()


def build_emotion(config: EmotionConfig, model: ChatModel) -> EmotionSystem:
    """Composition helper for app.py. Assumes config.enabled is already true."""
    return EmotionSystem(config, model)
