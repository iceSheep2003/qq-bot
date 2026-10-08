"""Long-term memory orchestration.

Responsibilities are split deliberately:

* collection   — :meth:`MemoryService.collect` pulls the recent transcript
* distillation — :meth:`MemoryService.distill` asks the model for candidate facts
* dedupe/conflict — :mod:`qunbot.memory.dedupe` plus ``MemoryStore.observe``
* retrieval    — ``MemoryStore.candidates`` (hard filters, raw signals)
* ranking      — :mod:`qunbot.memory.ranking`
* injection    — :meth:`MemoryService.related`, the only method the Agent calls

The module never imports an extension, a network client or a storage adapter.
Retrieval always degrades to lexical search when embeddings are unavailable.
"""

from __future__ import annotations

import json
import logging
import re
import time

from ..ports import ChatModel, ConversationRepository, MemoryRepository
from . import dedupe, ranking
from .embeddings import embeddings_from_env
from .graph import (
    GraphError,
    GraphExtractor,
    GraphSettings,
    fingerprint,
    settings_from_env,
)
from .models import (
    GROUP_SUBJECT,
    MemoryEvidence,
    MemoryProposal,
    VISIBILITY_GROUP,
    VISIBILITY_PERSONAL,
    RetrievalHit,
)

log = logging.getLogger(__name__)

COLLECT_LIMIT = 20
MAX_FACTS = 3
FACT_MIN_CHARS = 5
FACT_MAX_CHARS = 300
DEFAULT_CONFIDENCE = 0.65
MODEL_CONFIDENCE_CAP = 0.9
#: How long the newest message still counts as "who we are talking to now".
FOCUS_TTL_SECONDS = 900
MAINTAIN_INTERVAL_SECONDS = 300
EXTRACTION_STRATEGY_VERSION = "evidence-topics-v1"

EXTRACT_PROMPT = (
    "从聊天中提取最多3条明确、可长期保留的事实或偏好。不要推断私人敏感信息，"
    "不要记临时情绪。仅输出 JSON 数组，每项包含："
    "user_id（事实主体的QQ号，群整体事实用 \"_group_\"）、fact（一句话事实）、"
    "persona_hint（这条记忆只应如何影响自然语气，不能新增事实，最多60字）、"
    "mention_policy（direct|soft_echo|tone_only|avoid_unless_asked）、"
    "fact_type（fact|preference|relation|event|boundary|open_loop|promise）、"
    "confidence（0到1，把握不足就写低）、"
    "importance（1到5）、topic（简短稳定的话题名）、"
    "evidence_event_ids（支持该事实的消息ID数组）、"
    "supersedes（true 表示这条推翻了之前的旧说法）。"
    "消息每行以 [event_id] 开头；证据ID只能从输入选择。若无则输出 []。"
)


class DistillationError(ValueError):
    """The provider answered, but not with the extraction contract."""


def _field(row, key: str, default=None):
    if hasattr(row, "get"):
        value = row.get(key, default)
    else:  # sqlite3.Row
        value = row[key] if key in row.keys() else default
    return default if value is None else value


class MemoryService:
    def __init__(
        self,
        model: ChatModel,
        conversations: ConversationRepository,
        memories: MemoryRepository,
        embeddings=None,
        *,
        maintain_interval: int = MAINTAIN_INTERVAL_SECONDS,
        knowledge=None,
        graph_settings: GraphSettings | None = None,
    ):
        self.model = model
        self.conversations = conversations
        self.memories = memories
        # The entity/relation store is optional and off by default. Without it
        # the graph pass is a no-op, which is a supported deployment rather
        # than a degraded one: nothing reads the graph yet.
        self.knowledge = knowledge
        self.graph_settings = graph_settings or settings_from_env()
        self.extractor = (
            GraphExtractor(model, self.graph_settings)
            if knowledge is not None
            else None
        )
        # ``None`` means "whatever the environment configures"; pass ``False``
        # to force lexical-only retrieval.
        self.embeddings = embeddings_from_env() if embeddings is None else embeddings
        self.maintain_interval = maintain_interval
        self._last_maintain = 0.0
        if self.embeddings:
            attach = getattr(memories, "attach_embedder", None)
            if callable(attach):
                attach(self.embeddings)
                log.info("memory vector path enabled: %s", getattr(self.embeddings, "name", "?"))

    # ------------------------------------------------------------- injection

    def related(self, scope: str, query: str, limit: int = 4) -> list[str]:
        """The Agent-facing entry point. Returns content strings only."""
        try:
            focus = self._focus_subject(scope)
            hits = self.recall(scope, query, limit, subject_user_id=focus)
        except Exception:
            log.exception("memory retrieval failed for scope=%s; injecting nothing", scope)
            return []
        if not hits:
            return []
        for hit in hits:
            log.info(
                "memory injected scope=%s focus=%s %s why=[%s]",
                scope,
                focus or "-",
                hit.item.describe(),
                ", ".join(hit.reasons),
            )
        try:
            self.memories.reinforce([hit.item.id for hit in hits])
        except Exception:
            log.warning("memory reinforcement failed for scope=%s", scope, exc_info=True)
        return [hit.item.content for hit in hits]

    def related_context(self, scope: str, query: str, limit: int = 4) -> dict:
        """Structured dual-channel recall for natural, non-mechanical use.

        Canonical facts answer "what is known". Persona hints answer only "how
        may this affect tone". A mention policy prevents every recalled fact
        from being repeated aloud like a CRM record.
        """
        try:
            focus = self._focus_subject(scope)
            hits = self.recall(scope, query, limit, subject_user_id=focus)
        except Exception:
            log.exception("memory context retrieval failed for scope=%s", scope)
            return {"items": [], "usage_rule": "本轮不使用长期记忆"}
        items = []
        for hit in hits:
            item = hit.item
            items.append({
                "id": item.id,
                "fact": item.content,
                "type": item.fact_type,
                "persona_hint": item.persona_summary,
                "mention_policy": item.mention_policy,
                "confidence": (
                    "high" if item.confidence >= .8 else
                    "medium" if item.confidence >= .6 else "low"
                ),
            })
        if items:
            try:
                self.memories.reinforce([hit.item.id for hit in hits])
            except Exception:
                log.warning("memory reinforcement failed for scope=%s", scope, exc_info=True)
        return {
            "items": items,
            "usage_rule": (
                "记忆仅是背景事实，不是本轮话题。direct 可在确有帮助时自然提起；"
                "soft_echo 只轻描淡写地呼应；tone_only 只改变语气，禁止说出记忆内容；"
                "avoid_unless_asked 除非对方明确问起，否则不要提。"
            ),
        }

    def recall(
        self,
        scope: str,
        query: str,
        limit: int = 4,
        *,
        subject_user_id: str | None = None,
        include_candidates: bool = False,
        now: int | None = None,
    ) -> list[RetrievalHit]:
        """Retrieve + rank. ``subject_user_id`` is the person being talked to;
        memories about somebody else are not eligible."""
        candidates = self.memories.candidates(
            scope,
            query,
            subject_user_id=subject_user_id,
            include_candidates=include_candidates,
            now=now,
        )
        return ranking.rank(
            candidates, now=now, subject_user_id=subject_user_id, limit=limit
        )

    def explain(self, hits: list[RetrievalHit]) -> list[str]:
        return [hit.explain() for hit in hits]

    def _focus_subject(self, scope: str, now: int | None = None) -> str | None:
        """Who the bot is currently talking to in this scope, if fresh enough."""
        try:
            rows = self.conversations.recent(scope, 1)
        except Exception:
            log.debug("conversation lookup failed for scope=%s", scope, exc_info=True)
            return None
        if not rows:
            return None
        row = rows[-1]
        if str(_field(row, "role", "")) != "user":
            return None
        created = _field(row, "created_at")
        if created and (now or int(time.time())) - int(created) > FOCUS_TTL_SECONDS:
            return None
        return str(_field(row, "user_id", "") or "") or None

    # ------------------------------------------------------------- collection

    def collect(self, scope: str, limit: int = COLLECT_LIMIT) -> list[dict]:
        try:
            return list(self.conversations.recent(scope, limit))
        except Exception:
            log.exception("transcript collection failed for scope=%s", scope)
            return []

    def collect_incremental(self, scope: str, limit: int = COLLECT_LIMIT) -> list[dict]:
        cursor = getattr(self.memories, "extraction_cursor", lambda _scope: 0)(scope)
        after_id = getattr(self.conversations, "after_id", None)
        if not callable(after_id):
            return self.collect(scope, limit)
        try:
            return list(after_id(scope, cursor, limit))
        except Exception:
            log.exception("incremental transcript collection failed for scope=%s", scope)
            return []

    @staticmethod
    def render(rows: list[dict]) -> str:
        return "\n".join(
            f"[{_field(row, 'event_id', '')}] "
            f"{_field(row, 'nickname', '')}({_field(row, 'user_id', '')}): "
            f"{str(_field(row, 'content', ''))[:300]}"
            for row in rows
        )

    # ------------------------------------------------------------ distillation

    async def distill(self, transcript: str) -> list[dict]:
        """Ask the model for candidate facts. Returns [] on any malformed reply."""
        try:
            facts, _raw = await self._distill_payload(transcript)
            return facts
        except DistillationError:
            log.warning("memory distillation returned unparsable JSON")
            return []

    async def _distill_payload(self, transcript: str) -> tuple[list[dict], str]:
        if not transcript.strip():
            return [], "[]"
        result = await self.model.complete(
            [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": transcript},
            ],
            temperature=0,
        )
        content = result["choices"][0]["message"].get("content") or "[]"
        match = re.search(r"\[[\s\S]*\]", content)
        if not match:
            raise DistillationError("missing JSON array")
        try:
            facts = json.loads(match.group())
        except json.JSONDecodeError as exc:
            raise DistillationError("invalid JSON array") from exc
        if not isinstance(facts, list):
            raise DistillationError("root is not an array")
        return [item for item in facts if isinstance(item, dict)][:MAX_FACTS], content

    async def extract(self, scope: str) -> None:
        """One extraction pass: facts, then the graph if it is switched on.

        The graph runs in a ``finally`` because the two are independent reads
        of the same transcript, with their own watermarks. A fact pass that
        fails on a malformed reply must not also starve the graph, and vice
        versa.
        """
        try:
            await self._extract_facts(scope)
        finally:
            await self.extract_relations(scope)

    async def _extract_facts(self, scope: str) -> None:
        rows = self.collect_incremental(scope)
        if not rows:
            return
        first_id = int(_field(rows[0], "id", 0) or 0)
        last_id = int(_field(rows[-1], "id", 0) or 0)
        start_run = getattr(self.memories, "start_extraction", None)
        run_id = (
            start_run(
                scope, first_id, last_id, len(rows),
                strategy_version=EXTRACTION_STRATEGY_VERSION,
                model_name=str(getattr(self.model, "name", "") or ""),
            )
            if callable(start_run) else None
        )
        known_users = {
            str(_field(row, "user_id", "")) for row in rows if _field(row, "role") == "user"
        }
        by_event = {
            str(_field(row, "event_id", "")): row for row in rows
            if _field(row, "event_id", "")
        }
        statuses: dict[str, int] = {}
        rejected = 0
        raw = ""
        try:
            facts, raw = await self._distill_payload(self.render(rows))
            for item in facts:
                outcome = self._store_fact(scope, item, known_users, by_event, run_id)
                if outcome:
                    statuses[outcome] = statuses.get(outcome, 0) + 1
                else:
                    rejected += 1
        except DistillationError as exc:
            finish = getattr(self.memories, "finish_extraction", None)
            if callable(finish) and run_id is not None:
                finish(run_id, status="failed", raw_result=raw, error=str(exc))
            log.warning("memory extraction contract failed scope=%s: %s", scope, exc)
            return
        except Exception as exc:
            finish = getattr(self.memories, "finish_extraction", None)
            if callable(finish) and run_id is not None:
                finish(run_id, status="failed", raw_result=raw, error=str(exc))
            raise
        finish = getattr(self.memories, "finish_extraction", None)
        if callable(finish) and run_id is not None:
            finish(
                run_id, status="succeeded", raw_result=raw,
                accepted_count=sum(statuses.values()), rejected_count=rejected,
                advance_to=last_id,
            )
        if statuses:
            log.info("memory extract scope=%s outcomes=%s", scope, statuses)
        self.maintain()

    # ----------------------------------------------------------- graph pass

    async def extract_relations(self, scope: str) -> dict:
        """Extract entities and triples from new messages. No-op unless enabled.

        Its own watermark, apart from fact extraction's: both read the same
        transcript, but one falling behind must not stall the other. Two model
        calls per passage, and nothing is called at all until a passage is
        genuinely new.
        """
        if self.knowledge is None or self.extractor is None:
            return {}
        if not self.graph_settings.enabled:
            return {}

        rows = self._graph_transcript(scope)
        if not rows:
            return {}
        last_id = int(_field(rows[-1], "id", 0) or 0)
        transcript = self.render(rows)
        digest = self._passage_digest(rows)
        if self.knowledge.passage_seen(scope, digest):
            # The same text pasted into a new message. The graph already holds
            # what it says, and re-extracting it would only inflate counts.
            self.knowledge.advance_scan(scope, last_id)
            return {"skipped": "seen"}

        try:
            entities, triples = await self.extractor.extract(transcript)
        except GraphError as exc:
            # The watermark is deliberately not advanced: a contract failure
            # is worth retrying, unlike a passage we have already read.
            log.warning("graph extraction contract failed scope=%s: %s", scope, exc)
            return {}
        except Exception:
            log.exception("graph extraction failed for scope=%s", scope)
            return {}

        self.knowledge.mark_passage(scope, digest)
        result = self.knowledge.record(scope, entities, triples)
        self.knowledge.advance_scan(scope, last_id)
        if result.get("new_triples") or result.get("new_entities"):
            log.info("graph extract scope=%s %s", scope, result)
        return result

    @staticmethod
    def _passage_digest(rows: list[dict]) -> str:
        """Identity of a passage, taken from its text alone.

        Deliberately *not* the rendered transcript: that carries event ids, so
        the same words pasted into a new message would hash differently and be
        extracted all over again, which is the whole thing the ledger is for.
        """
        return fingerprint(
            "\n".join(str(_field(row, "content", "")) for row in rows)
        )

    def _graph_transcript(self, scope: str) -> list[dict]:
        cursor = getattr(self.knowledge, "last_scan", lambda _scope: 0)(scope)
        after_id = getattr(self.conversations, "after_id", None)
        if not callable(after_id):
            return []
        try:
            return list(after_id(scope, cursor, COLLECT_LIMIT))
        except Exception:
            log.exception("graph transcript collection failed for scope=%s", scope)
            return []

    def related_relations(self, scope: str, query: str, limit: int = 8) -> list[dict]:
        """Triples touching any entity this query names.

        Substring matching over the scope's entity list rather than a search:
        a group's entities are few, and the question is only whether a name
        appears in what was just said.
        """
        if self.knowledge is None:
            return []
        try:
            names = [
                str(row["name"])
                for row in self.knowledge.entities(scope, limit=200)
                if row["name"] and str(row["name"]) in query
            ]
            if not names:
                return []
            return self.knowledge.triples_about(scope, names, limit)
        except Exception:
            log.exception("relation lookup failed for scope=%s", scope)
            return []

    def _store_fact(
        self,
        scope: str,
        item: dict,
        known_users: set[str],
        by_event: dict[str, dict],
        run_id: int | None = None,
    ) -> str | None:
        subject = str(item.get("user_id", "")).strip()
        fact = str(item.get("fact", "")).strip()
        if not (FACT_MIN_CHARS <= len(fact) <= FACT_MAX_CHARS):
            return None
        group_level = subject in ("", GROUP_SUBJECT, "group", "all")
        if not group_level and subject not in known_users:
            return None
        try:
            confidence = float(item.get("confidence", DEFAULT_CONFIDENCE))
        except (TypeError, ValueError):
            confidence = DEFAULT_CONFIDENCE
        confidence = max(0.0, min(MODEL_CONFIDENCE_CAP, confidence))
        try:
            importance = max(1, min(5, int(item.get("importance", 1))))
        except (TypeError, ValueError):
            importance = 1
        requested_ids = item.get("evidence_event_ids") or []
        if not isinstance(requested_ids, list):
            requested_ids = []
        evidence_rows = [by_event[str(key)] for key in requested_ids if str(key) in by_event]
        if not evidence_rows:
            evidence_rows = [
                row for row in by_event.values()
                if group_level or str(_field(row, "user_id", "")) == subject
            ][-4:]
        evidence = tuple(
            MemoryEvidence(
                event_id=str(_field(row, "event_id", "")),
                user_id=str(_field(row, "user_id", "")),
                excerpt=str(_field(row, "content", ""))[:500],
                observed_at=int(_field(row, "created_at", 0) or 0),
            )
            for row in evidence_rows if _field(row, "event_id", "")
        )
        source_event_id = evidence[-1].event_id if evidence else None
        proposal = MemoryProposal(
            subject_user_id=GROUP_SUBJECT if group_level else subject,
            content=fact,
            persona_summary=str(item.get("persona_hint", "") or "")[:120],
            mention_policy=self._mention_policy(item, str(item.get("fact_type", "fact"))),
            fact_type=str(item.get("fact_type", "fact") or "fact"),
            confidence=confidence,
            importance=importance,
            visibility=VISIBILITY_GROUP if group_level else VISIBILITY_PERSONAL,
            topic=str(item.get("topic", "") or "")[:100],
            evidence_event_ids=tuple(value.event_id for value in evidence),
            supersedes=bool(item.get("supersedes")),
        )
        record = getattr(self.memories, "record_proposal", None)
        proposal_id = record(run_id, scope, proposal) if callable(record) else None
        result = self.remember(
            scope,
            proposal.subject_user_id,
            proposal.content,
            fact_type=proposal.fact_type,
            confidence=proposal.confidence,
            importance=proposal.importance,
            source_event_id=source_event_id,
            visibility=proposal.visibility,
            supersede=proposal.supersedes,
            evidence=evidence,
            topic=proposal.topic,
            persona_summary=proposal.persona_summary,
            mention_policy=proposal.mention_policy,
        )
        decide = getattr(self.memories, "decide_proposal", None)
        if callable(decide) and proposal_id is not None:
            decide(proposal_id, "accepted", result["outcome"], result["memory_id"])
        return result["outcome"]

    @staticmethod
    def _mention_policy(item: dict, fact_type: str) -> str:
        requested = str(item.get("mention_policy", "") or "")
        if requested in ("direct", "soft_echo", "tone_only", "avoid_unless_asked"):
            return requested
        return {
            "relation": "tone_only",
            "boundary": "avoid_unless_asked",
            "preference": "soft_echo",
            "open_loop": "direct",
            "promise": "direct",
        }.get(fact_type, "soft_echo")

    # ------------------------------------------------------------ write path

    def remember(
        self,
        scope: str,
        subject_user_id: str,
        content: str,
        *,
        fact_type: str = "fact",
        confidence: float = 1.0,
        importance: int = 1,
        source_event_id: str | None = None,
        origin_user_id: str | None = None,
        visibility: str = VISIBILITY_PERSONAL,
        expires_at: int | None = None,
        supersede: bool = False,
        evidence: tuple[MemoryEvidence, ...] = (),
        topic: str = "",
        persona_summary: str = "",
        mention_policy: str = "soft_echo",
    ) -> dict:
        """Idempotent write with a fuzzy near-duplicate check in front of it.

        A paraphrase reinforces the stored wording instead of adding a row; a
        contradiction retires the old row and stores the new one.
        """
        content = content.strip()[:FACT_MAX_CHARS]
        if not content:
            raise ValueError("empty memory")
        existing = self.memories.similar_candidates(scope, subject_user_id)
        decision = dedupe.decide(existing, content)
        if decision.memory_id is not None and decision.kind in (
            dedupe.DECISION_DUPLICATE,
            dedupe.DECISION_PARAPHRASE,
        ):
            stored = next(
                (row for row in existing if row.get("id") == decision.memory_id), None
            )
            if stored is not None:
                log.debug(
                    "memory %s restated (score=%.2f), reinforcing instead of inserting",
                    decision.memory_id,
                    decision.score,
                )
                if dedupe.contradiction(str(stored.get("content", "")), content):
                    supersede = True
                else:
                    result = self.memories.observe(
                        scope,
                        subject_user_id,
                        str(stored.get("content", "")),
                        confidence=confidence,
                        fact_type=fact_type,
                        source_event_id=source_event_id,
                        origin_user_id=origin_user_id,
                        visibility=visibility,
                        persona_summary=persona_summary,
                        mention_policy=mention_policy,
                    )
                    return self._decorate_memory(
                        scope, result, evidence, topic, source_event_id,
                        action="reinforced", old_content=str(stored.get("content", "")),
                    )
        result = self.memories.observe(
            scope, subject_user_id, content, fact_type=fact_type,
            confidence=confidence, importance=importance,
            source_event_id=source_event_id, origin_user_id=origin_user_id,
            visibility=visibility, expires_at=expires_at, supersede=supersede,
            persona_summary=persona_summary,
            mention_policy=mention_policy,
        )
        return self._decorate_memory(
            scope, result, evidence, topic, source_event_id,
            action=result.get("outcome", "inserted"), new_content=content,
        )

    def _decorate_memory(
        self, scope: str, result: dict, evidence: tuple[MemoryEvidence, ...],
        topic: str, source_event_id: str | None, *, action: str,
        old_content: str = "", new_content: str = "",
    ) -> dict:
        memory_id = int(result["memory_id"])
        attach = getattr(self.memories, "attach_evidence", None)
        if callable(attach) and evidence:
            attach(memory_id, evidence)
        record = getattr(self.memories, "record_revision", None)
        if callable(record):
            record(
                memory_id, action, old_content=old_content,
                new_content=new_content, reason="memory write path",
                source_event_id=source_event_id,
            )
        upsert_topic = getattr(self.memories, "upsert_topic", None)
        link_topic = getattr(self.memories, "link_topic", None)
        if topic and callable(upsert_topic) and callable(link_topic):
            topic_id = upsert_topic(
                scope, topic, summary=new_content or old_content,
                confidence=float(result.get("confidence", 0.5))
            )
            if topic_id is not None:
                link_topic(memory_id, topic_id)
        return result

    # ------------------------------------------------------------- lifecycle

    def maintain(self, now: int | None = None, *, force: bool = False) -> dict:
        """Expire, promote, archive, purge — throttled so it can be called from
        the reply path without a scheduler dependency."""
        now = int(now or time.time())
        if not force and now - self._last_maintain < self.maintain_interval:
            return {}
        self._last_maintain = now
        try:
            result = self.memories.maintain(now)
        except Exception:
            log.exception("memory maintenance failed")
            return {}
        if any(result.values()):
            log.info("memory maintenance %s", result)
        return result

    def forget_memory(self, memory_id: int) -> int:
        removed = self.memories.forget([memory_id])
        log.info("memory forgotten id=%s removed=%s", memory_id, removed)
        return removed

    def forget_person(self, scope: str, subject_user_id: str) -> int:
        removed = self.memories.forget_subject(scope, subject_user_id)
        log.info("memory forgotten scope=%s subject=%s removed=%s", scope, subject_user_id, removed)
        return removed

    def forget_scope(self, scope: str) -> int:
        removed = self.memories.forget_scope(scope)
        log.info("memory forgotten scope=%s removed=%s", scope, removed)
        return removed

    def stats(self, scope: str | None = None) -> dict:
        return self.memories.stats(scope)

    # ------------------------------------------------------------- read model

    def overview(self, scope: str | None = None) -> dict:
        """Stable application-facing summary for a future UI/API adapter."""
        summary = dict(self.stats(scope))
        runs = self.extraction_runs(scope, 1)
        summary["latest_extraction"] = runs[0] if runs else None
        summary["topics"] = len(self.topics(scope, 1000))
        return summary

    def memory_detail(self, memory_id: int) -> dict | None:
        getter = getattr(self.memories, "get", None)
        if not callable(getter):
            return None
        item = getter(memory_id)
        if item is None:
            return None
        public = dict(item)
        public.pop("embedding", None)
        evidence = getattr(self.memories, "evidence", None)
        revisions = getattr(self.memories, "revisions", None)
        public["evidence"] = evidence(memory_id) if callable(evidence) else []
        public["revisions"] = revisions(memory_id) if callable(revisions) else []
        return public

    def topics(self, scope: str | None = None, limit: int = 100) -> list[dict]:
        reader = getattr(self.memories, "topics", None)
        return reader(scope, limit) if callable(reader) else []

    def extraction_runs(
        self, scope: str | None = None, limit: int = 50
    ) -> list[dict]:
        reader = getattr(self.memories, "extraction_runs", None)
        return reader(scope, limit) if callable(reader) else []
