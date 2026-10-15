"""Stable behavior-graph proposal contracts.

These are proposals, not executable plans and not durable claims about people.
The vocabulary is intentionally closed so a model cannot invent ontology types.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


Strategy = Literal[
    "answer_directly",
    "acknowledge",
    "agree",
    "empathize",
    "banter",
    "tease_gently",
    "supplement",
    "clarify",
    "encourage",
    "deescalate",
    "topic_shift",
    "celebrate",
    "abstain",
]

DialogueAct = Literal[
    "answer",
    "acknowledge",
    "agree",
    "empathize",
    "joke",
    "tease",
    "add_information",
    "clarify",
    "encourage",
    "calm",
    "bridge_topic",
    "celebrate",
    "wait",
]

MediaIntent = Literal[
    "none",
    "meme",
    "voice",
    "text_plus_meme",
    "text_plus_voice",
    "native_reaction",
]

Outcome = Literal["continued", "resolved", "uncertain"]


class BehaviorProposal(BaseModel):
    """One abstracted interaction pattern grounded in one source case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=160)
    source_message_ids: list[str] = Field(min_length=1, max_length=12)
    situation: str = Field(min_length=1, max_length=120)
    observable_cues: list[str] = Field(max_length=6)
    strategy: Strategy
    dialogue_act: DialogueAct
    expression_pattern: str = Field(min_length=1, max_length=240)
    avoid: list[str] = Field(max_length=6)
    media_intent: MediaIntent
    topic_terms: list[str] = Field(max_length=6)
    outcome: Outcome
    abstraction: str = Field(min_length=1, max_length=360)


class BehaviorBatch(BaseModel):
    """Bounded model output for a batch of cases."""

    model_config = ConfigDict(extra="forbid")

    proposals: list[BehaviorProposal] = Field(max_length=16)
