"""The temporary style delta, as a closed vocabulary rather than free text.

This module is pure: no clock, no model, no I/O. Everything is a deterministic
function of its arguments, which is what makes the whole feature testable
without a network and makes the rendered sentence byte-identical for identical
input (the idempotency requirement rests on that).

The shape is a *closed vocabulary* on purpose. The model is allowed to answer
only with a small fixed set of switches, and this module — not the model —
writes the Chinese sentence that reaches the prompt. A per-turn persona that
accepted free text from a model whose input includes group chat would be an
open channel from "what a group member typed" to "how the bot behaves", which
the project's boundaries forbid. With this design the worst a fully successful
prompt injection can do is toggle ``exclaim`` off for the TTL window.

Nothing here is a personality. A strategy says how this stretch of conversation
is phrased, never who the bot is: the stable persona file stays the only source
of identity.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Closed vocabularies. A value outside these sets is not "clamped", it is a
# rejected proposal — a model that did not answer in-vocabulary does not get to
# have its answer half-applied.
LENGTHS = ("normal", "terse")
TONES = ("neutral", "calm", "warm", "lively")

# The ceiling the context registry is told about. The rendered sentence is far
# shorter than this; the number is a bound, not a target.
MAX_RENDERED_CHARS = 200

_PHRASES = {
    ("length", "terse"): "话短一点，一句能说完就别展开",
    ("tone", "calm"): "语气平和些",
    ("tone", "warm"): "语气温和亲近些",
    ("tone", "lively"): "语气活泼一点",
    ("exclaim", False): "别用感叹号",
}

_HEADER = "这会儿的表达方式："
# Says out loud what the whole package promises: this is tone for the moment,
# not a new character, and the model is told not to treat it as one.
_FOOTER = "。（只影响这一小段时间的语气，不改变你一贯的性格。）"


def _clamp(text: str) -> str:
    if len(text) <= MAX_RENDERED_CHARS:
        return text
    return text[: MAX_RENDERED_CHARS - 1] + "…"


@dataclass(frozen=True)
class StyleStrategy:
    """One stretch of conversation's phrasing, bounded to three switches."""

    length: str = "normal"
    tone: str = "neutral"
    exclaim: bool = True

    def is_default(self) -> bool:
        return (
            self.length == "normal"
            and self.tone == "neutral"
            and self.exclaim is True
        )

    def render(self) -> str:
        """The bounded Chinese guidance, or "" for "no deviation".

        An empty string is the honest answer for the default strategy: adding a
        line that says "speak normally" would spend context budget to tell the
        model what it already does.
        """
        parts: list[str] = []
        if self.length == "terse":
            parts.append(_PHRASES[("length", "terse")])
        if self.tone in ("calm", "warm", "lively"):
            parts.append(_PHRASES[("tone", self.tone)])
        if self.exclaim is False:
            parts.append(_PHRASES[("exclaim", False)])
        if not parts:
            return ""
        return _clamp(_HEADER + "，".join(parts) + _FOOTER)


# Every field defaults to "no deviation", so the default strategy renders to
# nothing at all and the turn falls back to the baseline persona.
DEFAULT = StyleStrategy()


def parse_strategy(content: str) -> StyleStrategy | None:
    """Extract an in-vocabulary strategy. ``None`` means "drop this silently".

    Unknown keys are ignored rather than fatal — a model that volunteers an
    extra field is not wrong, it just is not followed. A *known* key holding an
    out-of-vocabulary value rejects the whole proposal, because that is the
    signature of an answer that is not about phrasing at all.
    """
    match = re.search(r"\{[\s\S]*\}", content or "")
    if not match:
        return None
    try:
        proposal = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(proposal, dict):
        return None
    strategy = StyleStrategy()
    for key, value in proposal.items():
        if key == "length":
            if value not in LENGTHS:
                return None
            strategy = StyleStrategy(
                length=value, tone=strategy.tone, exclaim=strategy.exclaim
            )
        elif key == "tone":
            if value not in TONES:
                return None
            strategy = StyleStrategy(
                length=strategy.length, tone=value, exclaim=strategy.exclaim
            )
        elif key == "exclaim":
            if not isinstance(value, bool):
                return None
            strategy = StyleStrategy(
                length=strategy.length, tone=strategy.tone, exclaim=value
            )
    return strategy
