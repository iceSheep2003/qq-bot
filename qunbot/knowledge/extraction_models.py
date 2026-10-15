"""Typed extraction proposals. Distinct from durable ontology documents."""

from __future__ import annotations
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ExtractionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1, max_length=160)
    subject_id: str = Field(min_length=1, max_length=160)
    predicate: Literal[
        "likes",
        "dislikes",
        "goal",
        "relationship",
        "experience",
        "commitment",
        "topic",
        "definition",
    ]
    value: str = Field(min_length=1, max_length=600)
    quote: str = Field(min_length=1, max_length=2000)
    source_kind: Literal["self_report", "hearsay", "inferred"]
    modality: Literal["assertion", "plan", "joke", "hypothesis"]
    polarity: Literal["positive", "negative", "uncertain"]
    confidence: float = Field(ge=0, le=1)


class SlangReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1, max_length=160)
    term: str = Field(min_length=1, max_length=80)
    definition: str = Field(min_length=1, max_length=600)
    quote: str = Field(min_length=1, max_length=2000)


class ExtractionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reports: list[ExtractionReport] = Field(max_length=16)
    slang: list[SlangReport] = Field(max_length=4)
