"""Cheap, platform-native social gestures that do not need an LLM."""

from qqbot.actions.interactions.reactions import ReactionPolicy, ReactionSettings
from qqbot.actions.interactions.lightweight import LightAction, LightInteractionPolicy, LightInteractionSettings
from qqbot.actions.interactions.pokes import PokeController, PokeSettings

__all__ = [
    "LightAction", "LightInteractionPolicy", "LightInteractionSettings",
    "PokeController", "PokeSettings", "ReactionPolicy", "ReactionSettings",
]
