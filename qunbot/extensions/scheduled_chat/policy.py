"""Configuration for post-message interval continuation.

Two layers, both local files owned by the deployer:

* ``ProactiveConfig`` — deployment-wide defaults, read from the environment the
  same way ``qunbot/emotion`` reads its own. The core ``Config`` stays free of
  feature fields and this file is deleted along with the package.
* ``GroupPolicySet`` — a per-group JSON file for the fine-grained behaviour the
  global active-hours window cannot express: per-group do-not-disturb hours,
  per-group cooldown, freshness and probability, and a per-group mute.

Neither layer is reachable from chat. A group member can neither silence the
bot nor make it speak more; only the deployer edits these files.

The dataclass defaults are deliberately the *permissive* ones — no quiet
window, no do-not-disturb hours, no mute. A ``ProactiveConfig`` built in code
therefore never silently suppresses a post, and a test can exercise one gate
without arranging for the others to be open. ``from_env`` is what a deployment
actually gets, and it applies the real defaults.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_GROUPS_PATH = "./config/proactive_groups.json"

# Anything the scheduler's ``every`` kind will accept. The same value paces the
# in-process worker tick, so one knob controls how finely a quiet window
# resolves on either trigger path.
MIN_CHECK_SECONDS = 300

_GROUP_FIELDS = {
    "enabled": bool,
    "quiet_start_hour": int,
    "quiet_end_hour": int,
    "quiet_minutes": int,
    "interval_minutes": int,
    "retry_minutes": int,
    "freshness_minutes": int,
    "daily_limit": int,
    "probability": float,
    "max_chars": int,
    "min_messages": int,
}


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _int(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _ratio(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


@dataclass(frozen=True)
class ProactiveConfig:
    # A deployment may keep the extension loaded but quiet.
    enabled: bool = True
    # Minimum minutes between two proactive posts — the floor that keeps the
    # bot from crowding a conversation it just joined.
    interval_minutes: int = 0
    daily_limit: int = 2
    # How long the group has to be silent before a continuation is considered.
    quiet_minutes: int = 0
    # Past this much silence the recent chatter is too old to continue from.
    freshness_minutes: int = 180
    # Minimum gap between two attempts inside one quiet window. It grows with
    # the silent streak, capped by the freshness window.
    retry_minutes: int = 30
    # Chance of speaking once every other gate has passed. This is the "should
    # this turn be one where the bot pipes up at all" roll, not a content
    # decision — the model still gets to stay silent afterwards.
    probability: float = 0.15
    min_messages: int = 3
    max_chars: int = 150
    # Per-group do-not-disturb hours. ``None`` means the group has none.
    quiet_start_hour: int | None = None
    quiet_end_hour: int | None = None
    check_seconds: int = MIN_CHECK_SECONDS
    groups_path: Path = Path(DEFAULT_GROUPS_PATH)

    @classmethod
    def from_env(cls) -> ProactiveConfig:
        return cls(
            enabled=_flag("BOT_PROACTIVE_ENABLED", True),
            interval_minutes=_int(
                "BOT_PROACTIVE_INTERVAL_MINUTES", 180, low=0, high=10080
            ),
            daily_limit=_int("BOT_PROACTIVE_DAILY_LIMIT", 2, low=0, high=50),
            quiet_minutes=_int("BOT_PROACTIVE_QUIET_MINUTES", 12, low=0, high=1440),
            freshness_minutes=_int(
                "BOT_PROACTIVE_FRESHNESS_MINUTES", 180, low=1, high=10080
            ),
            retry_minutes=_int("BOT_PROACTIVE_RETRY_MINUTES", 30, low=0, high=1440),
            probability=_ratio("BOT_PROACTIVE_PROBABILITY", 0.15),
            min_messages=_int("BOT_PROACTIVE_MIN_MESSAGES", 3, low=1, high=200),
            max_chars=_int("BOT_PROACTIVE_MAX_CHARS", 150, low=20, high=1000),
            quiet_start_hour=_int(
                "BOT_PROACTIVE_QUIET_START_HOUR", 23, low=0, high=23
            ),
            quiet_end_hour=_int("BOT_PROACTIVE_QUIET_END_HOUR", 8, low=0, high=24),
            check_seconds=max(
                MIN_CHECK_SECONDS,
                _int("BOT_PROACTIVE_CHECK_SECONDS", 300, low=30, high=86400),
            ),
            groups_path=Path(
                os.getenv("BOT_PROACTIVE_GROUPS_PATH", DEFAULT_GROUPS_PATH)
            ),
        )


@dataclass(frozen=True)
class GroupPolicy:
    """One group's overrides. ``None`` means "use the deployment default"."""

    enabled: bool = True
    quiet_start_hour: int | None = None
    quiet_end_hour: int | None = None
    quiet_minutes: int | None = None
    interval_minutes: int | None = None
    retry_minutes: int | None = None
    freshness_minutes: int | None = None
    daily_limit: int | None = None
    probability: float | None = None
    max_chars: int | None = None
    min_messages: int | None = None

    def apply(self, base: ProactiveConfig) -> ProactiveConfig:
        overrides = {
            name: getattr(self, name)
            for name in _GROUP_FIELDS
            if name != "enabled" and getattr(self, name) is not None
        }
        return replace(base, **overrides) if overrides else base


class GroupPolicySet:
    """The per-group policy file: ``{"groups": {"<id>": {...}}}``."""

    def __init__(self, groups: dict[str, GroupPolicy] | None = None):
        self.groups = groups or {}

    @classmethod
    def load(cls, path: Path, *, required: bool = False) -> GroupPolicySet:
        if not path.exists():
            if required:
                raise ValueError(f"proactive group policy file not found: {path}")
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from None
        if not isinstance(raw, dict):
            raise TypeError(f"{path} must contain a JSON object")
        entries = raw.get("groups", {})
        if not isinstance(entries, dict):
            raise TypeError(f"{path}: 'groups' must be an object")
        groups: dict[str, GroupPolicy] = {}
        for group_id, entry in entries.items():
            groups[str(group_id)] = cls._parse(str(path), str(group_id), entry)
        return cls(groups)

    @staticmethod
    def _parse(where: str, group_id: str, entry: object) -> GroupPolicy:
        if not isinstance(entry, dict):
            raise TypeError(f"{where}: group {group_id!r} must be an object")
        unknown = set(entry) - set(_GROUP_FIELDS)
        if unknown:
            raise ValueError(
                f"{where}: group {group_id!r} has unknown keys {sorted(unknown)}"
            )
        values: dict[str, object] = {}
        for name, value in entry.items():
            kind = _GROUP_FIELDS[name]
            if kind is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if kind is bool and isinstance(value, bool):
                values[name] = value
                continue
            if kind is bool or not isinstance(value, kind):
                raise ValueError(
                    f"{where}: group {group_id!r} key {name!r} must be "
                    f"{'true/false' if kind is bool else kind.__name__}"
                )
            values[name] = value
        policy = GroupPolicy(**values)
        _check_range(where, group_id, "quiet_start_hour", policy.quiet_start_hour, 0, 23)
        _check_range(where, group_id, "quiet_end_hour", policy.quiet_end_hour, 0, 24)
        _check_range(where, group_id, "probability", policy.probability, 0.0, 1.0)
        for name in ("quiet_minutes", "interval_minutes", "retry_minutes",
                     "freshness_minutes", "daily_limit", "max_chars", "min_messages"):
            _check_range(where, group_id, name, getattr(policy, name), 0, 10080)
        return policy

    def get(self, group_id: str) -> GroupPolicy:
        return self.groups.get(group_id, GroupPolicy())

    def resolve(self, group_id: str, base: ProactiveConfig) -> ProactiveConfig | None:
        """The effective config, or ``None`` when this group is muted."""
        policy = self.get(group_id)
        if not policy.enabled:
            return None
        return policy.apply(base)


def _check_range(where, group_id, name, value, low, high) -> None:
    if value is None:
        return
    if not low <= value <= high:
        raise ValueError(
            f"{where}: group {group_id!r} key {name!r} must be between {low} and {high}"
        )


def in_quiet_hours(start: int | None, end: int | None, hour: int) -> bool:
    """Do-not-disturb check that handles a window wrapping past midnight."""
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end
