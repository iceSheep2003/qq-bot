"""Concrete construction and ownership of the built-in mood feature."""
import time
from qqbot.agent.identity.emotion.config import EmotionConfig
from qqbot.agent.identity.emotion.system import EmotionSystem
from qqbot.contracts.ports import ChatModel
from qqbot.infrastructure.storage.emotion import EmotionStore


def build_emotion(config: EmotionConfig, model: ChatModel, *, clock=time.time) -> EmotionSystem:
    store = EmotionStore(config.db_path)
    try:
        return EmotionSystem(config, model, store=store, clock=clock)
    except BaseException:
        store.close()
        raise
