"""Internal scene routing; private experience never inherits a group graph."""

import logging
import os
from dataclasses import dataclass

from ..conversation.channels import channel_for
from ..experience.decision import GraphDecision, LlmDecision, MODES
from ..experience.runtime import configured_behavior_experience

log = logging.getLogger(__name__)

_PRIVATE_KEYS = {
    "BOT_BEHAVIOR_GRAPH_PATH": "BOT_PRIVATE_BEHAVIOR_GRAPH_PATH",
    "BOT_BEHAVIOR_CALIBRATION_PATH": "BOT_PRIVATE_BEHAVIOR_CALIBRATION_PATH",
    "BOT_BEHAVIOR_MAX_OPTIONS": "BOT_PRIVATE_BEHAVIOR_MAX_OPTIONS",
    "BOT_BEHAVIOR_MAX_CASES": "BOT_PRIVATE_BEHAVIOR_MAX_CASES",
    "BOT_BEHAVIOR_CHAR_BUDGET": "BOT_PRIVATE_BEHAVIOR_CHAR_BUDGET",
    "BOT_BEHAVIOR_PRIOR_WEIGHT": "BOT_PRIVATE_BEHAVIOR_PRIOR_WEIGHT",
}


@dataclass(frozen=True)
class SceneRoutes:
    group: object
    private: object

    def decide_for(self, event, query):
        route = self.group if channel_for(event) == "group" else self.private
        return route.decide(query)


def _route(raw, settings=None):
    mode = raw.strip().lower()
    if mode not in MODES or mode == "off":
        return LlmDecision()
    experience = configured_behavior_experience(settings=settings)
    if experience is None:
        log.warning("No usable %s experience snapshot; using the direct route", mode)
        return LlmDecision()
    return GraphDecision(experience, mode)


def configured_scene_routes():
    group_mode = os.getenv("BOT_GROUP_BEHAVIOR_MODE", "").strip() or os.getenv("BOT_BEHAVIOR_MODE", "off")
    private_mode = os.getenv("BOT_PRIVATE_BEHAVIOR_MODE", "off")
    private_settings = {key: os.getenv(name, "") for key, name in _PRIVATE_KEYS.items()}
    return SceneRoutes(_route(group_mode), _route(private_mode, private_settings))
