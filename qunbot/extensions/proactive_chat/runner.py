"""The ``proactive_chat`` background extension: a tick for the continuation.

The decision logic lives in :mod:`qunbot.extensions.scheduled_chat.engine` and
the knobs in :mod:`qunbot.extensions.scheduled_chat.policy`. That placement is
deliberate: ``extensions/loader.py`` only lets an extension in ``JOB_EXTENSIONS``
register a job action, and the scheduler's ``continuation`` action is the
canonical trigger, so the engine belongs with the action that owns it. This
module imports it and turns the environment into a running worker.

Enabling ``scheduled_chat`` therefore does not import this package at all — the
background trigger is only built when ``proactive_chat`` is in
``BOT_EXTENSIONS``. Both paths drive the same engine over the same persisted
quiet window, so enabling both cannot make the bot post twice.
"""

from __future__ import annotations

from ...ports import MoodObserver
from ..scheduled_chat.engine import (
    DEFAULT_PROMPT,
    Decision,
    ProactiveChat,
    mood_gate,
)
from ..scheduled_chat.policy import GroupPolicySet, ProactiveConfig

__all__ = [
    "DEFAULT_PROMPT",
    "Decision",
    "GroupPolicySet",
    "ProactiveChat",
    "ProactiveConfig",
    "build_worker",
    "mood_gate",
]


def build_worker(bot, gateway, config, proactive_gate: MoodObserver | None):
    """Factory contract of ``BACKGROUND_EXTENSIONS``.

    ``config`` is the core ``Config``; the feature reads its own environment in
    ``ProactiveConfig.from_env`` rather than growing fields on the core.
    """
    proactive = ProactiveConfig.from_env()
    policies = GroupPolicySet.load(proactive.groups_path)
    engine = ProactiveChat(bot, proactive, proactive_gate, policies)
    return engine.loop(lambda: bool(getattr(gateway, "connection", None)))
