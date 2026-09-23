"""Reply-policy settings, read from the environment like every other extension.

These knobs live here rather than in ``qunbot/config.py`` on purpose: the core
Config stays free of feature business fields, and this file disappears with the
package when the feature is not wanted.

Everything defaults to *off* and to the safest mode. A deployment that exports
nothing keeps the built-in "@ me only" rule.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# "mention_only" is the built-in rule expressed as a policy (useful for replay);
# "room" is the read-the-room heuristic. Anything else is a startup error rather
# than a silent fallback, so a typo cannot quietly change how the bot behaves.
MODES = ("mention_only", "room")


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _number(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _names(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class ReplyPolicyConfig:
    """Immutable so a running policy cannot be retuned from chat or in flight."""

    enabled: bool = False
    mode: str = "mention_only"
    # Score at or above which the room-reading policy speaks. Higher is quieter.
    threshold: float = 0.5
    # Do not answer someone else's chatter right after the bot's own turn.
    cooldown_seconds: int = 20
    # How long the bot still counts as "part of this conversation".
    engaged_window_seconds: int = 600
    # A burst of messages is a room talking to itself, not to the bot.
    burst_seconds: int = 20
    burst_count: int = 5
    # This many distinct speakers in a row means a crowd; stay out of it.
    crowd_speakers: int = 3
    # Names that address the bot in message text. Empty means the name signal is
    # simply inactive, which is the privacy-preserving default.
    bot_names: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "ReplyPolicyConfig":
        mode = os.getenv("BOT_REPLY_POLICY_MODE", "mention_only").strip().lower()
        if mode not in MODES:
            raise ValueError(f"BOT_REPLY_POLICY_MODE must be one of {MODES}")
        return cls(
            enabled=_flag("BOT_REPLY_POLICY_ENABLED", False),
            mode=mode,
            threshold=_number("BOT_REPLY_POLICY_THRESHOLD", 0.5, low=0.0, high=1.0),
            cooldown_seconds=int(
                _number("BOT_REPLY_POLICY_COOLDOWN_SECONDS", 20, low=0, high=3600)
            ),
            engaged_window_seconds=int(
                _number("BOT_REPLY_POLICY_ENGAGED_SECONDS", 600, low=0, high=86400)
            ),
            burst_seconds=int(
                _number("BOT_REPLY_POLICY_BURST_SECONDS", 20, low=1, high=3600)
            ),
            burst_count=int(
                _number("BOT_REPLY_POLICY_BURST_COUNT", 5, low=2, high=100)
            ),
            crowd_speakers=int(
                _number("BOT_REPLY_POLICY_CROWD_SPEAKERS", 3, low=2, high=50)
            ),
            bot_names=_names("BOT_REPLY_POLICY_BOT_NAMES"),
        )
