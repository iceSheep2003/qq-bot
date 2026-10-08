"""The persona extension reads its own environment, like ``emotion/`` does.

Keeping these knobs here rather than in ``qunbot/config.py`` is deliberate: the
core ``Config`` stays free of persona fields, and deleting this package deletes
its configuration with it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _integer(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class PersonaConfig:
    enabled: bool
    ttl_minutes: int
    # Scheduled scans are separately enabled because they spend model calls.
    # Auto mode replaces only the marked example window, never the core rules.
    proposals_enabled: bool = False
    proposal_interval_hours: int = 6
    proposal_min_messages: int = 20
    proposal_window_messages: int = 60
    auto_examples_enabled: bool = False

    @classmethod
    def from_env(cls) -> PersonaConfig:
        return cls(
            # Off unless the deployer asks for it: this feature spends a model
            # call and adds a second voice to the prompt, so it is never on by
            # accident.
            enabled=_flag("BOT_PERSONA_ENABLED", False),
            ttl_minutes=_integer(
                "BOT_PERSONA_TTL_MINUTES", 30, low=1, high=1440
            ),
            proposals_enabled=_flag("BOT_PERSONA_PROPOSALS_ENABLED", False),
            proposal_interval_hours=_integer(
                "BOT_PERSONA_PROPOSAL_INTERVAL_HOURS", 6, low=1, high=168
            ),
            proposal_min_messages=_integer(
                "BOT_PERSONA_PROPOSAL_MIN_MESSAGES", 20, low=5, high=1000
            ),
            proposal_window_messages=_integer(
                "BOT_PERSONA_PROPOSAL_WINDOW_MESSAGES", 60, low=10, high=500
            ),
            auto_examples_enabled=_flag("BOT_PERSONA_AUTO_EXAMPLES_ENABLED", False),
        )
