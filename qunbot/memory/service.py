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
from .models import (
    GROUP_SUBJECT,
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

EXTRACT_PROMPT = (
    "从聊天中提取最多3条明确、可长期保留的事实或偏好。不要推断私人敏感信息，"
    "不要记临时情绪。仅输出 JSON 数组，每项包含："
    "user_id（事实主体的QQ号，群整体事实用 \"_group_\"）、fact（一句话事实）、"
    "fact_type（fact|preference|relation|event|boundary）、"
    "confidence（0到1，把握不足就写低）、"
    "supersedes（true 表示这条推翻了之前的旧说法）。若无则输出 []。"
)


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
    ):
        self.model = model
        self.conversations = conversations
        self.memories = memories
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

    @staticmethod
    def render(rows: list[dict]) -> str:
        return "\n".join(
            f"{_field(row, 'nickname', '')}({_field(row, 'user_id', '')}): "
            f"{str(_field(row, 'content', ''))[:300]}"
            for row in rows
        )

    # ------------------------------------------------------------ distillation

    async def distill(self, transcript: str) -> list[dict]:
        """Ask the model for candidate facts. Returns [] on any malformed reply."""
        if not transcript.strip():
            return []
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
            return []
        try:
            facts = json.loads(match.group())
        except json.JSONDecodeError:
            log.warning("memory distillation returned unparsable JSON")
            return []
        return [item for item in facts if isinstance(item, dict)][:MAX_FACTS] if isinstance(facts, list) else []

    async def extract(self, scope: str) -> None:
        rows = self.collect(scope)
        if not rows:
            return
        known_users = {
            str(_field(row, "user_id", "")) for row in rows if _field(row, "role") == "user"
        }
        source_event_id = str(_field(rows[-1], "event_id", "") or "") or None
        statuses = {}
        for item in await self.distill(self.render(rows)):
            outcome = self._store_fact(scope, item, known_users, source_event_id)
            if outcome:
                statuses[outcome] = statuses.get(outcome, 0) + 1
        if statuses:
            log.info("memory extract scope=%s outcomes=%s", scope, statuses)
        self.maintain()

    def _store_fact(
        self,
        scope: str,
        item: dict,
        known_users: set[str],
        source_event_id: str | None,
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
        return self.remember(
            scope,
            GROUP_SUBJECT if group_level else subject,
            fact,
            fact_type=str(item.get("fact_type", "fact") or "fact"),
            confidence=confidence,
            source_event_id=source_event_id,
            visibility=VISIBILITY_GROUP if group_level else VISIBILITY_PERSONAL,
            supersede=bool(item.get("supersedes")),
        )["outcome"]

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
                    return self.memories.observe(
                        scope,
                        subject_user_id,
                        str(stored.get("content", "")),
                        confidence=confidence,
                        fact_type=fact_type,
                        source_event_id=source_event_id,
                        origin_user_id=origin_user_id,
                        visibility=visibility,
                    )
        return self.memories.observe(
            scope,
            subject_user_id,
            content,
            fact_type=fact_type,
            confidence=confidence,
            importance=importance,
            source_event_id=source_event_id,
            origin_user_id=origin_user_id,
            visibility=visibility,
            expires_at=expires_at,
            supersede=supersede,
        )

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
