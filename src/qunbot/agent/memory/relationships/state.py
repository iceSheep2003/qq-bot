"""Relationship state as pure values and pure functions.

Two things live in this module and nowhere else:

* the *facts* the bot knows about a person (kept in the repository, see
  ``storage/relationships.py``: per-group nickname, card, first seen);
* the *interaction state* between the bot and that person in one group — a
  bounded score plus the relationship stage it currently falls into.

The score never reaches the model as a number. Everything the agent is allowed
to see comes from :meth:`RelationshipPolicy.narrate`, which is bounded prose.
Stages, bounds, cooldowns and the anti-farming caps are all constructor
arguments, so a deployment can retune wording without touching the schema.

No database, no HTTP client, no model. Everything here is a deterministic
function of its arguments.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Existing constraints, preserved verbatim: the -100..100 range, a delta of
# -3..3 excluding 0, a mandatory reason, and one change per person per group
# per hour.
FLOOR, CEIL = -100, 100
MIN_DELTA, MAX_DELTA = -3, 3
COOLDOWN_SECONDS = 3600

# The automatic evaluator is deliberately narrower than the repository: an
# interaction is worth at most two points. Manual writes may use -3..3.
AUTO_DELTAS = frozenset({-2, -1, 1, 2})

# Anti-farming: even inside the cooldown, one person cannot move their own
# score indefinitely. A day is a rolling 24h window.
MAX_EVENTS_PER_DAY = 8
MAX_POSITIVE_PER_DAY = 5
DAY_SECONDS = 86400

# Trusted provenance values. A group message is never a source; only the
# post-reply evaluator or an explicit deployment-side write may change a score.
SOURCES = frozenset({"evaluator", "manual", "import"})


@dataclass(frozen=True)
class Stage:
    """One relationship band. ``floor`` is inclusive; bands are open above."""

    key: str
    label: str
    floor: int
    address: str
    guidance: str


# Descending floors from +60 down to -100. First band whose floor the score
# reaches wins, so the tuple must cover the whole -100..100 range.
DEFAULT_STAGES: tuple[Stage, ...] = (
    Stage(
        "trusted",
        "很亲近",
        60,
        "可以叫昵称",
        "语气亲近随意，可以主动关心、接梗，也可以开温和的玩笑",
    ),
    Stage(
        "close",
        "熟络",
        30,
        "自然称呼",
        "语气放松，可以聊些私事、接梗，偶尔主动搭话",
    ),
    Stage(
        "familiar",
        "认识",
        10,
        "正常称呼",
        "语气自然友好，可以寒暄，不做亲昵表达",
    ),
    Stage(
        "neutral",
        "普通群友",
        -10,
        "正常称呼",
        "自然友好，可以接日常话题和轻松的梗；调侃要轻，不追着一个人连续开玩笑",
    ),
    Stage(
        "distant",
        "有点疏远",
        -30,
        "保持距离",
        "礼貌但简短，少开玩笑，不主动搭话",
    ),
    Stage(
        "wary",
        "戒备",
        -60,
        "保持距离",
        "只做必要回应，不主动搭话，不追问私人话题",
    ),
    Stage(
        "hostile",
        "有敌意",
        FLOOR,
        "保持距离",
        "保持克制，只回答必要信息，不争执、不反击",
    ),
)


@dataclass(frozen=True)
class RelationshipPolicy:
    """Bounds, cadence, stages and wording. Stateless; safe to share."""

    floor: int = FLOOR
    ceil: int = CEIL
    min_delta: int = MIN_DELTA
    max_delta: int = MAX_DELTA
    cooldown_seconds: int = COOLDOWN_SECONDS
    max_events_per_day: int = MAX_EVENTS_PER_DAY
    max_positive_per_day: int = MAX_POSITIVE_PER_DAY
    auto_deltas: frozenset[int] = field(default=AUTO_DELTAS)
    stages: tuple[Stage, ...] = DEFAULT_STAGES
    narration_max_chars: int = 200
    # Dormancy decay. 0 disables it, and that is the default: a score that
    # drifts down on its own changes how the bot treats someone who has done
    # nothing, which is a deployer's decision rather than a sensible default.
    # When enabled, a relationship untouched for `grace` seconds starts moving
    # back toward neutral with this half-life.
    decay_half_life_seconds: int = 0
    decay_grace_seconds: int = 14 * DAY_SECONDS
    decay_max_rows: int = 500

    def clamp(self, value: int) -> int:
        return max(self.floor, min(self.ceil, int(value)))

    def stage_for(self, score: int) -> Stage:
        for stage in sorted(self.stages, key=lambda item: item.floor, reverse=True):
            if score >= stage.floor:
                return stage
        return min(self.stages, key=lambda item: item.floor)

    def validate_delta(self, delta: int) -> None:
        if not isinstance(delta, int) or isinstance(delta, bool):
            raise ValueError("affection delta must be an integer")
        if not self.min_delta <= delta <= self.max_delta or delta == 0:
            raise ValueError("affection delta must be -3..3, excluding 0")

    def narrate(self, score: int) -> str:
        """The only relationship text the agent may see. Never a number."""
        stage = self.stage_for(score)
        return f"关系阶段：{stage.label}。{stage.guidance}。"[: self.narration_max_chars]

    def guidance(self, stage_key: str) -> str:
        for stage in self.stages:
            if stage.key == stage_key:
                return stage.guidance
        raise KeyError(stage_key)


def policy_from_env() -> RelationshipPolicy:
    """The policy this deployment runs with.

    Only dormancy decay is environmental. The stage bands, the daily caps and
    the cooldown stay constants the module was designed around: two
    deployments that ran different numbers would no longer be comparable, and
    the caps are what makes an automatic scorer safe to leave on.
    """
    raw = os.getenv("BOT_AFFECTION_DECAY_HALF_LIFE_SECONDS", "").strip()
    if not raw:
        return RelationshipPolicy()
    try:
        half_life = int(float(raw))
    except ValueError:
        raise ValueError(
            "BOT_AFFECTION_DECAY_HALF_LIFE_SECONDS must be a number of seconds"
        ) from None
    if half_life < 0:
        raise ValueError(
            "BOT_AFFECTION_DECAY_HALF_LIFE_SECONDS cannot be negative"
        )
    return RelationshipPolicy(decay_half_life_seconds=half_life)


@dataclass(frozen=True)
class AffectionProposal:
    """A *proposed* change. Writing it is a separate, permissioned step."""

    group_id: str
    user_id: str
    delta: int
    reason: str
    source: str = "evaluator"
    source_event_id: str | None = None

    def validate(self, policy: RelationshipPolicy) -> None:
        if not self.group_id or not self.user_id:
            raise ValueError("affection proposal needs a group and a user")
        if self.source not in SOURCES:
            raise ValueError(f"unknown affection source: {self.source!r}")
        policy.validate_delta(self.delta)
        if not self.reason.strip():
            raise ValueError("affection change requires a reason")

    def normalized(self) -> AffectionProposal:
        return AffectionProposal(
            group_id=self.group_id,
            user_id=self.user_id,
            delta=self.delta,
            reason=self.reason.strip()[:240],
            source=self.source,
            source_event_id=self.source_event_id,
        )
