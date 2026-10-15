"""Behavior experience bounded context.

This package deliberately models *how a group companion participates* rather
than facts about historical group members.  The extraction side produces
evidence-backed candidates; publication is a separate step.
"""

from .models import BehaviorBatch, BehaviorProposal
from .retrieval import BehaviorOption, BehaviorRetriever
from .compiler import BehaviorCompiler, BehaviorContext, CompiledBehavior
from .graph_retrieval import BehaviorGraphRetriever
from .runtime import CompiledBehaviorExperience, configured_behavior_experience

__all__ = [
    "BehaviorBatch", "BehaviorProposal", "BehaviorOption", "BehaviorRetriever",
    "BehaviorCompiler", "BehaviorContext", "CompiledBehavior",
    "BehaviorGraphRetriever",
    "CompiledBehaviorExperience", "configured_behavior_experience",
]
