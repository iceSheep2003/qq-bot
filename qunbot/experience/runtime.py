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

from .compiler import BehaviorCompiler
from .graph_retrieval import BehaviorGraphRetriever

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
        if not isinstance(query, str) or not query.strip():
            return ""
        try:
            options = self.retriever.retrieve(query[:2400], limit=3)
            if not options:
                return ""
            return self.compiler.compile(options).render()
        except Exception as exc:
            log.warning(
                "behavior experience retrieval failed (%s: %s); contributing nothing",
                type(exc).__name__,
                exc,
            )
            return ""


def configured_behavior_experience() -> CompiledBehaviorExperience | None:
    """Load the optional behavior snapshot from deployment configuration.

    No default path points at private data.  A missing or malformed optional
    snapshot disables the read model with a warning instead of preventing the
    bot from starting.
    """

    raw_snapshot = os.getenv("BOT_BEHAVIOR_GRAPH_PATH", "").strip()
    if not raw_snapshot:
        return None
    snapshot = Path(raw_snapshot).expanduser()
    raw_calibration = os.getenv("BOT_BEHAVIOR_CALIBRATION_PATH", "").strip()
    calibration = Path(raw_calibration).expanduser() if raw_calibration else None
    try:
        retriever = BehaviorGraphRetriever(snapshot, calibration=calibration)
        compiler = BehaviorCompiler(
            max_options=max(1, min(3, int(os.getenv("BOT_BEHAVIOR_MAX_OPTIONS", "3")))),
            max_cases=max(0, min(2, int(os.getenv("BOT_BEHAVIOR_MAX_CASES", "2")))),
            char_budget=max(240, min(2400, int(os.getenv("BOT_BEHAVIOR_CHAR_BUDGET", "1200")))),
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("Behavior graph disabled: %s", type(exc).__name__)
        return None
    log.info("Behavior graph enabled: %s", snapshot)
    return CompiledBehaviorExperience(retriever, compiler)

