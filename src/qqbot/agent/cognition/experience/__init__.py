"""Behavior experience bounded context.

This package deliberately models *how a group companion participates* rather
than facts about historical group members.  The extraction side produces
evidence-backed candidates; publication is a separate step.
"""

from qqbot.agent.cognition.experience.models import BehaviorBatch, BehaviorProposal
from qqbot.agent.cognition.experience.retrieval import BehaviorOption, BehaviorRetriever
from qqbot.agent.cognition.experience.compiler import BehaviorCompiler, BehaviorContext, CompiledBehavior
from qqbot.agent.cognition.experience.graph_retrieval import BehaviorGraphRetriever
from qqbot.agent.cognition.experience.runtime import CompiledBehaviorExperience, configured_behavior_experience

__all__ = [
    "BehaviorBatch", "BehaviorProposal", "BehaviorOption", "BehaviorRetriever",
    "BehaviorCompiler", "BehaviorContext", "CompiledBehavior",
    "BehaviorGraphRetriever",
    "CompiledBehaviorExperience", "configured_behavior_experience",
]
