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
        )
