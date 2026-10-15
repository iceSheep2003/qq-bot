"""Runtime adapter for the offline behavior graph.

The graph is an optional read model.  This module is the anti-corruption layer
between the compiled JSONL snapshot and the chat agent: it exposes only a
bounded text context, never evidence rows, identities or executable actions.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from qunbot.agent.cognition.experience.compiler import BehaviorCompiler, BehaviorContext
from qunbot.agent.cognition.experience.graph_retrieval import DEFAULT_PRIOR_WEIGHT, BehaviorGraphRetriever

log = logging.getLogger(__name__)


class CompiledBehaviorExperience:
    """Turn graph candidates into a small model-facing behavior hint."""

    def __init__(
        self,
        retriever: BehaviorGraphRetriever,
        compiler: BehaviorCompiler | None = None,
    ):
        self.retriever = retriever
        self.compiler = compiler or BehaviorCompiler(
            max_options=3, max_cases=2, char_budget=1200
        )

    def context_for(self, query: str) -> str:
        """A bounded hint for this turn, or ``""`` when there is nothing to say.

        Never raises. This is the anti-corruption layer: a snapshot that loaded
        cleanly can still fail on a particular query — an unexpected node shape,
        a missing field on one of 2000 cases — and the graph's problem must not
        become the reply's problem. An empty string is the honest answer in that
        case: the agent answers from the scene, memory and skills alone.
        """
        context = self.compiled_for(query)
        return "" if context is None else context.render()

    def compiled_for(self, query: str) -> BehaviorContext | None:
        """The compiled options for this turn, or ``None`` when there are none.

        ``context_for`` returns rendered text; the binding route needs the
        fields (strategy, action, avoid) as well, and both must come from the
        same retrieval so a hint and a decision can never disagree.
        """
        if not isinstance(query, str) or not query.strip():
            return None
        try:
            options = self.retriever.retrieve(query[:2400], limit=self.compiler.max_options)
            if not options:
                return None
            return self.compiler.compile(options)
        except Exception as exc:
            log.warning(
                "behavior experience retrieval failed (%s: %s); contributing nothing",
                type(exc).__name__,
                exc,
            )
            return None


def configured_behavior_experience(*, settings=None) -> CompiledBehaviorExperience | None:
    """Load the optional behavior snapshot from deployment configuration.

    No default path points at private data.  A missing or malformed optional
    snapshot disables the read model with a warning instead of preventing the
    bot from starting.
    """

    get = os.getenv if settings is None else settings.get
    raw_snapshot = get("BOT_BEHAVIOR_GRAPH_PATH", "").strip()
    if not raw_snapshot:
        return None
    snapshot = Path(raw_snapshot).expanduser()
    raw_calibration = get("BOT_BEHAVIOR_CALIBRATION_PATH", "").strip()
    calibration = Path(raw_calibration).expanduser() if raw_calibration else None
    # The default is ``None`` — calibration off (see ``DEFAULT_PRIOR_WEIGHT``).
    # An empty value keeps that default; ``off``/``none`` spell it out; anything
    # else is parsed as a weight. ``str(DEFAULT_PRIOR_WEIGHT)`` is deliberately
    # not used here: it would produce the string ``"None"`` and disable the graph.
    raw_weight = get("BOT_BEHAVIOR_PRIOR_WEIGHT", "").strip()
    try:
        if not raw_weight:
            prior_weight = DEFAULT_PRIOR_WEIGHT
        elif raw_weight.lower() in ("off", "none", "null"):
            prior_weight = None
        else:
            prior_weight = float(raw_weight)
        retriever = BehaviorGraphRetriever(
            snapshot, calibration=calibration, prior_weight=prior_weight
        )
        compiler = BehaviorCompiler(
            max_options=max(1, min(3, int(get("BOT_BEHAVIOR_MAX_OPTIONS", "3") or "3"))),
            max_cases=max(0, min(2, int(get("BOT_BEHAVIOR_MAX_CASES", "2") or "2"))),
            char_budget=max(240, min(2400, int(get("BOT_BEHAVIOR_CHAR_BUDGET", "1200") or "1200"))),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("Behavior graph disabled: %s", type(exc).__name__)
        return None
    log.info(
        "Behavior graph enabled: %s (prior weight %s)", snapshot, retriever.prior_weight
    )
    return CompiledBehaviorExperience(retriever, compiler)

