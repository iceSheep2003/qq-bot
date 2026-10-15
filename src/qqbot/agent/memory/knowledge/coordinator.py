"""Agent-facing knowledge coordinator. Legacy storage remains owner-readable.

Only the new ontology path influences live recall; legacy memory tables are
not silently promoted into accepted claims without valid evidence.
"""

from __future__ import annotations
import asyncio
import logging

from qqbot.contracts.ontology.objects import stable_id
from qqbot.agent.memory.context import MemoryContextPolicy
from qqbot.agent.memory.scope import ConversationScope

log = logging.getLogger(__name__)


class OntologyMemory:
    def __init__(
        self, knowledge, extractor, projector, conversations, legacy, *, runs=None,
        context_policy=None,
    ):
        self.ontology = knowledge
        self.extractor, self.projector = extractor, projector
        self.conversations, self.legacy = conversations, legacy
        self.memories = legacy.memories  # Explicit owner-console archive adapter.
        self._locks = {}
        self.runs = runs
        self.context_policy = context_policy or MemoryContextPolicy.from_env()

    def related(self, scope, query, limit=4):
        return [h["summary"] for h in self.ontology.search(scope, query, limit)]

    def related_context(self, scope, query, limit=4):
        hits = self.ontology.search(scope, query, max(1, min(4, limit)))
        # Topic candidates are NOT automatically imported from the archive or
        # promoted to accepted knowledge. This live path has its own evidence
        # rules and uses exactly the same full-envelope budget as legacy recall.
        return self.context_policy.assemble(hits, [])

    def context_for_turn(self, event, query, limit=4):
        scope = ConversationScope.validate(event.scope)
        subject = ConversationScope.subject_key(scope, event.user_id)
        hits = self.ontology.search(
            scope, query, max(1, min(4, limit)), subject_id=subject
        )
        return self.context_policy.assemble(hits, [])

    def context_for_group_turn(self, scope, query, limit=4):
        """Recall only shared vocabulary/topics for proactive group speech.

        A proactive turn has no human addressee.  Falling back to the bot's
        own identity would hide useful group context; removing the subject
        filter would leak one member's private claim to the whole room.  Topic
        and slang predicates are the safe middle ground.
        """
        scope = ConversationScope.validate(scope)
        hits = []
        for category in ("topic", "slang"):
            hits.extend(self.ontology.search(
                scope, query, max(1, min(4, limit)), category=category
            ))
        unique = {item["claim_id"]: item for item in hits}
        ordered = sorted(unique.values(), key=lambda item: (-float(item.get("score", 0)), item["claim_id"]))
        return self.context_policy.assemble(ordered[: max(1, min(4, limit))], [])

    async def extract(self, scope):
        async with self._locks.setdefault(scope, asyncio.Lock()):
            self.projector.process(100)
            cursor = self.conversations.knowledge_cursor(scope)
            rows = self.conversations.after_id(scope, cursor, 40)
            if not rows:
                return
            messages = []
            for row in rows:
                node = self.ontology.graph.get(
                    scope, stable_id("message", scope, row["event_id"])
                )
                if node:
                    messages.append(node)
            if len(messages) != len(rows):
                return  # Projection lag cannot advance the semantic cursor.
            run = (
                self.runs.begin(scope, rows, self.extractor.VERSION)
                if self.runs
                else None
            )
            try:
                result = await self.extractor.extract(scope, messages)
                advanced = self.conversations.advance_knowledge_cursor(
                    scope, int(rows[-1]["id"]), expected=cursor
                )
                if advanced is False:
                    raise RuntimeError("Extraction cursor changed")
            except BaseException:
                if run:
                    # Never persist exception text from the model/provider.
                    self.runs.finish(
                        run, error="提炼中断或结果未提交；消息游标不会跳过该批次。"
                    )
                raise
            if run:
                self.runs.finish(run, result)

    def overview(self, *args, **kwargs):
        return self.legacy.overview(*args, **kwargs)

    def memory_detail(self, *args, **kwargs):
        return self.legacy.memory_detail(*args, **kwargs)

    def topics(self, *args, **kwargs):
        return self.legacy.topics(*args, **kwargs)

    def extraction_runs(self, *args, **kwargs):
        return self.runs.list_runs(*args, **kwargs) if self.runs else []
