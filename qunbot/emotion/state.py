"""The bot's own affect as pure values and pure functions.

No database, no HTTP client, no model. Everything here is a deterministic
function of its arguments, which is what makes the whole emotion system
testable without a clock or a network — a caller with a fixed clock and a fixed
list of events gets byte-identical answers every time, which is also what makes
:func:`replay` able to rebuild a past mood exactly.

This module owns *state*, never other people's facts. It reads no group
member's affection, writes none, and cannot touch the stable persona.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

# Five dimensions, each 0..100 and relaxed toward BASELINE when nothing happens.
# Deliberately no libido/aggression axes: this bot shares a study group and
# must never turn hostile, so there is nothing here that could express that.
DIMENSIONS = ("valence", "energy", "stress", "interest", "sociability")
BASELINE = 50.0
FLOOR, CEIL = 0.0, 100.0
MAX_DELTA = 15

# Coupling matrix: a dimension drags its neighbours by a fraction of its own
# deviation from BASELINE. Small on purpose — a mood should seep, not snap.
COUPLING: dict[str, dict[str, float]] = {
    "stress": {"energy": -0.18, "valence": -0.12, "interest": -0.08},
    "energy": {"interest": 0.12, "sociability": 0.10},
    "valence": {"stress": -0.15, "sociability": 0.10},
    "interest": {"sociability": 0.12},
    "sociability": {"valence": 0.06},
}

# Descending thresholds -> wording. First match wins, so each tuple ends at 0.
# These strings are the only thing the model ever sees; the numbers stay here.
BANDS: dict[str, tuple[tuple[float, str], ...]] = {
    "valence": (
        (70, "心情很好"),
        (55, "心情不错"),
        (45, "心情平静"),
        (30, "有点低落"),
        (0, "很低落"),
    ),
    "energy": (
        (70, "精神头很足"),
        (55, "还算有精神"),
        (45, "精力一般"),
        (30, "有点累"),
        (0, "很疲惫"),
    ),
    "stress": (
        (70, "绷得很紧"),
        (55, "有点压力"),
        (45, "压力还好"),
        (30, "比较放松"),
        (0, "很放松"),
    ),
    "interest": (
        (70, "兴致很高"),
        (55, "有点兴致"),
        (45, "兴致平平"),
        (30, "提不起兴致"),
        (0, "完全没兴致"),
    ),
    "sociability": (
        (70, "很想说话"),
        (55, "愿意聊两句"),
        (45, "跟平时一样"),
        (30, "不太想说话"),
        (0, "只想安静待着"),
    ),
}

MOOD_HINT = (
    "（以上心境只影响你说话的语气、长短和想不想主动开口。"
    "不要提及任何数值，不要因此失礼、迁怒或辱骂任何人。）"
)


def clamp(value: float) -> float:
    return max(FLOOR, min(CEIL, value))


@dataclass(frozen=True)
class Mood:
    """One scope's affect, as stored plus the moment it was last written."""

    valence: float = BASELINE
    energy: float = BASELINE
    stress: float = BASELINE
    interest: float = BASELINE
    sociability: float = BASELINE
    reason: str = ""
    updated_at: int = 0

    def values(self) -> dict[str, float]:
        return {name: float(getattr(self, name)) for name in DIMENSIONS}

    @classmethod
    def from_row(cls, row: dict) -> Mood:
        """Rebuild from a stored row. An absent row means an untouched mood."""
        if not row:
            return cls()
        return cls(
            **{name: float(row[name]) for name in DIMENSIONS},
            reason=str(row.get("reason") or ""),
            updated_at=int(row.get("updated_at") or 0),
        )


@dataclass(frozen=True)
class MoodEvent:
    """One recorded change, read back from the event log.

    ``at`` is the instant the change was *applied*, which is also the instant
    the mood's clock was reset to — that equality is what makes replay exact
    rather than approximate. ``deltas`` are the raw proposal, before coupling,
    so replay runs the same arithmetic the live path ran.
    """

    at: int
    deltas: dict[str, int] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class MoodSnapshot:
    """The mood as it stood immediately after one event."""

    at: int
    mood: Mood
    deltas: dict[str, int] = field(default_factory=dict)
    reason: str = ""


def replay(policy: "EmotionPolicy", events, *, upto: int | None = None) -> Mood:
    """Rebuild the mood from the event log.

    ``events`` must be in log order (the store returns them by ascending id,
    which is the order they were applied). Folding them through the same
    :meth:`EmotionPolicy.apply` the live path used reproduces the stored state
    exactly, provided the policy's constants are unchanged.

    ``upto`` answers the other question — "what was the mood *then*?" — by
    ignoring every event that happened later and decaying forward to that
    instant. Without it you get the mood as of the last recorded change.
    """
    ordered = tuple(events)
    if upto is not None:
        ordered = tuple(event for event in ordered if event.at <= upto)
    mood = Mood(updated_at=ordered[0].at) if ordered else Mood()
    for event in ordered:
        mood = policy.apply(mood, event.deltas, event.reason, event.at)
    return policy.decay(mood, upto) if upto is not None else mood


def timeline(policy: "EmotionPolicy", events) -> tuple[MoodSnapshot, ...]:
    """Every past mood the log can account for, oldest first."""
    ordered = tuple(events)
    mood = Mood(updated_at=ordered[0].at) if ordered else Mood()
    snapshots: list[MoodSnapshot] = []
    for event in ordered:
        mood = policy.apply(mood, event.deltas, event.reason, event.at)
        snapshots.append(
            MoodSnapshot(
                at=event.at,
                mood=mood,
                deltas=dict(event.deltas),
                reason=event.reason,
            )
        )
    return tuple(snapshots)


def _gain_matrix(coupling: float) -> tuple[tuple[float, ...], ...]:
    """``I + coupling*C``: how one apply() maps a deviation from baseline."""
    return tuple(
        tuple(
            (1.0 if source == target else 0.0)
            + coupling * COUPLING.get(source, {}).get(target, 0.0)
            for target in DIMENSIONS
        )
        for source in DIMENSIONS
    )


def _spectral_radius(matrix: tuple[tuple[float, ...], ...]) -> float:
    """Largest |eigenvalue|, by power iteration (the dominant one is positive)."""
    size = len(matrix)
    vector = [1.0] * size
    for _ in range(500):
        moved = [sum(row[j] * vector[j] for j in range(size)) for row in matrix]
        norm = math.sqrt(sum(value * value for value in moved))
        if norm == 0.0:
            return 0.0
        vector = [value / norm for value in moved]
    moved = [sum(row[j] * vector[j] for j in range(size)) for row in matrix]
    return math.sqrt(sum(value * value for value in moved))


def loop_gain(policy: "EmotionPolicy", *, cadence_seconds: int) -> float:
    """How much a deviation from baseline survives one event cycle.

    One :meth:`EmotionPolicy.apply` multiplies a deviation by ``I + coupling*C``
    and the decay that runs first multiplies it by ``exp(-T/tau)`` for an event
    cadence ``T``. A product **above 1** means the mood is a latch instead of a
    mood: any steady stream of events, even a balanced one, grows until a
    dimension reaches 0 or 100 and stays there. A product below 1 means the
    decay wins and the mood hovers near the baseline.

    This is the calibration check for ``BOT_MOOD_DECAY_MINUTES`` and
    ``BOT_MOOD_COUPLING``; ``tests/test_emotion.py`` asserts the shipped pair.
    """
    return _spectral_radius(_gain_matrix(policy.coupling)) * math.exp(
        -cadence_seconds / policy.tau_seconds
    )


class EmotionPolicy:
    """Decay, coupling and wording. Stateless; safe to share."""

    def __init__(
        self,
        *,
        half_life_minutes: int = 180,
        sensitivity: float = 1.0,
        min_sociability: int = 35,
        baseline: float = BASELINE,
        coupling: float = 1.0,
    ):
        self.half_life_minutes = half_life_minutes
        self.sensitivity = sensitivity
        self.min_sociability = min_sociability
        self.baseline = baseline
        # Multiplier on COUPLING. 1.0 is the shipped feel; see ``loop_gain``
        # for why a deployer who wants a mood that hovers instead of latching
        # sets it to 0.
        self.coupling = coupling
        # e-folding time. A value halfway back to baseline after half_life.
        self.tau_seconds = (half_life_minutes * 60.0) / math.log(2)

    def decay(self, mood: Mood, now: int) -> Mood:
        """Relax toward baseline. Pure: never writes back, so re-reading is
        stable and the bot can sleep for hours with no background ticker."""
        elapsed = now - mood.updated_at
        if elapsed <= 0:
            return mood
        factor = math.exp(-elapsed / self.tau_seconds)
        spread = {
            name: self.baseline + (getattr(mood, name) - self.baseline) * factor
            for name in DIMENSIONS
        }
        return replace(mood, updated_at=now, **spread)

    def apply(
        self, mood: Mood, deltas: dict[str, int], reason: str, now: int
    ) -> Mood:
        """Decay, add bounded deltas, let the coupling matrix settle, clamp."""
        current = self.decay(mood, now)
        spread = current.values()
        for name, raw in deltas.items():
            if name in DIMENSIONS:
                spread[name] = clamp(
                    spread[name] + int(raw) * self.sensitivity
                )
        for source, targets in COUPLING.items():
            deviation = spread[source] - self.baseline
            for target, weight in targets.items():
                spread[target] = clamp(
                    spread[target] + weight * self.coupling * deviation
                )
        return Mood(
            **spread,
            reason=reason.strip()[:120],
            updated_at=now,
        )

    def narrate(self, mood: Mood) -> str:
        """Render the mood as prose. Bands keep every number out of the prompt."""
        parts = [self._band(name, getattr(mood, name)) for name in DIMENSIONS]
        lines = ["当前心境：" + "，".join(parts[:2]) + "。" + "，".join(parts[2:]) + "。"]
        if mood.reason.strip():
            lines.append(f"最近的心事：{mood.reason.strip()}。")
        lines.append(MOOD_HINT)
        return "\n".join(lines)

    def permits_proactive(self, mood: Mood) -> bool:
        """False when the bot is too withdrawn to start a conversation."""
        return mood.sociability >= self.min_sociability

    def _band(self, name: str, value: float) -> str:
        for threshold, wording in BANDS[name]:
            if value >= threshold:
                return wording
        return BANDS[name][-1][1]
