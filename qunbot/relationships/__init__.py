"""Per-group relationship state: the sole owner of affection data.

Two layers are kept apart here:

* facts about a person (nickname per group, card, first seen) live in
  ``storage/relationships.py``'s ``group_members`` table;
* the interaction state (a bounded score, its stage, and an audit trail) lives
  in ``relations`` / ``affection_events``.

The package exposes pure policy (:mod:`state`) and the scoring proposal layer
(:mod:`evaluator`). Writes go through the repository's ``apply_proposal``;
nothing else may create a second affection table.
"""

from .evaluator import AffectionEvaluator, parse_proposal
from .state import (
    AUTO_DELTAS,
    CEIL,
    DEFAULT_STAGES,
    FLOOR,
    SOURCES,
    AffectionProposal,
    RelationshipPolicy,
    Stage,
)

__all__ = [
    "AUTO_DELTAS",
    "AffectionEvaluator",
    "AffectionProposal",
    "CEIL",
    "DEFAULT_STAGES",
    "FLOOR",
    "RelationshipPolicy",
    "SOURCES",
    "Stage",
    "parse_proposal",
]
