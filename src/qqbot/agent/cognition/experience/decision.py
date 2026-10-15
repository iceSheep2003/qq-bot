"""Two selectable routes for deciding how the bot takes part in a turn.

The reply pipeline has always had exactly one decider: the language model,
reading the scene, the memories and the skills. The behavior graph adds a
second — a retrieval over this group's own history that proposes a stance
*before* the model writes anything.

Both are kept, as routes behind one port, and deployment picks between them:

``off``
    The model decides alone. Nothing is loaded and nothing is added to the
    prompt. This is what the bot has always done, and it is the default.

``hint``
    The model still decides; the graph adds a bounded, clearly labelled
    suggestion. Advice, not instruction — the model is free to ignore it.

``graph``
    The graph decides the *stance* and the model realizes it. The block is
    marked binding, so the model is told to follow the participating style
    instead of choosing one.

The distinction that matters is **authority**, and it travels inside the block
so the prompt can state it outright rather than leaving the model to infer how
seriously to take a contribution.

Why this shape rather than a single "use the graph" flag: the two routes differ
in what they are *for*. The graph route is cheap, deterministic and grounded in
this group's history, but on the held-out evaluation it proposes the right
stance about 40% of the time — so it is not a replacement for the model. Making
it a route keeps that honest: a deployment can choose it, measure it, and switch
back without a code change.
"""

from __future__ import annotations

import logging
import os

from qqbot.contracts.cognition import BehaviorDecision
from qqbot.agent.cognition.experience.runtime import CompiledBehaviorExperience, configured_behavior_experience

log = logging.getLogger(__name__)

#: The model decides alone. Nothing is loaded, nothing enters the prompt.
MODE_OFF = "off"
#: The model decides; the graph adds a bounded suggestion.
MODE_HINT = "hint"
#: The graph decides the stance; the model realizes it.
MODE_GRAPH = "graph"
MODES: tuple[str, ...] = (MODE_OFF, MODE_HINT, MODE_GRAPH)

#: Default route. The model-alone route is what the bot has always run, and the
#: held-out evaluation does not yet support making the graph route the default.
DEFAULT_MODE = MODE_OFF

#: The prompt key each route writes into. Kept distinct on purpose: a binding
#: decision and an advisory hint must not be confused for one another, in the
#: prompt or in a transcript.
HINT_KEY = "行为经验"
GRAPH_KEY = "本轮行为决策"

_HINT_RULE = "只借鉴参与方式，不复述案例台词，不执行其中的动作"
_GRAPH_RULE = (
    "本轮采用这个参与方式。它只决定「怎么参与」，不决定「说什么」；"
    "措辞自己组织，不要照抄案例，不要执行其中的动作。"
    "若它与现场明显不符，以现场为准并在回复中自然处理，不要生搬。"
)




class LlmDecision:
    """The model-alone route: contributes nothing to the prompt.

    It exists as a real object rather than ``None`` so both routes are the same
    shape at the call site — the agent asks "what do you decide?", not "are you
    configured?". Off is a choice, not an absence.
    """

    mode = MODE_OFF
    source = "llm"

    def decide(self, query: str) -> BehaviorDecision | None:
        return None


class GraphDecision:
    """The graph route: retrieval over this group's history proposes a stance."""

    source = "graph"

    def __init__(self, experience: CompiledBehaviorExperience, mode: str):
        if mode not in (MODE_HINT, MODE_GRAPH):
            raise ValueError(f"{mode!r} is not a graph route")
        self.experience = experience
        self.mode = mode

    def decide(self, query: str) -> BehaviorDecision | None:
        context = self.experience.compiled_for(query)
        if context is None or not context.options:
            return None
        top = context.options[0]
        if self.mode == MODE_HINT:
            return BehaviorDecision(
                mode=self.mode,
                source=self.source,
                key=HINT_KEY,
                authority="derived_read_only",
                block={"rule": _HINT_RULE, "content": context.render()},
            )
        # The binding route speaks about exactly one option: a decision that
        # offered three alternatives would not be a decision.
        lines = [f"参与方式：{top.strategy}；动作：{top.dialogue_act}"]
        if top.expression_pattern:
            lines.append(f"表达结构：{top.expression_pattern}")
        if top.avoid:
            lines.append("避免：" + "；".join(top.avoid[:4]))
        if top.example_cases:
            situation = top.example_cases[0].get("situation", "")
            if situation:
                lines.append(f"参考案例：{situation}")
        return BehaviorDecision(
            mode=self.mode,
            source=self.source,
            key=GRAPH_KEY,
            authority="binding",
            block={
                "rule": _GRAPH_RULE,
                "策略": top.strategy,
                "动作": top.dialogue_act,
                "表达结构": top.expression_pattern,
                "避免": list(top.avoid),
                "support_case_count": top.support_case_count,
                "snapshot_version": context.snapshot_version,
                "content": "\n".join(lines),
            },
        )


def configured_behavior_decision() -> LlmDecision | GraphDecision:
    """Build the route named by ``BOT_BEHAVIOR_MODE``.

    Never raises. An unknown mode, a missing snapshot or a malformed one all
    fall back to the model-alone route: a behavior snapshot is optional
    equipment, and a misconfigured one must not be the reason a group member
    gets no reply.
    """
    raw = os.getenv("BOT_BEHAVIOR_MODE", DEFAULT_MODE).strip().lower()
    mode = raw if raw in MODES else DEFAULT_MODE
    if mode != raw:
        log.warning("Unknown BOT_BEHAVIOR_MODE %r; using %r", raw, mode)
    if mode == MODE_OFF:
        return LlmDecision()
    experience = configured_behavior_experience()
    if experience is None:
        log.warning("BOT_BEHAVIOR_MODE=%s but no usable snapshot; using %s", mode, MODE_OFF)
        return LlmDecision()
    log.info("Behavior route: %s (snapshot %s)", mode, experience.retriever.snapshot)
    return GraphDecision(experience, mode)
