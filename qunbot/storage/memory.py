"""Scoped long-term memory rows.

This module owns the *write path* (idempotent, conflict aware) and *retrieval
signals* (lexical rank, vector cosine). Ordering and explanation live in
``qunbot.memory.ranking``; distillation lives in ``qunbot.memory.service``.
Nothing here imports a model client or a network library: an embedding
provider is attached duck-typed and every failure degrades to lexical search.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from array import array

from ..memory.text import query_tokens
from .base import SqliteRepository

log = logging.getLogger(__name__)

#: Subject used for facts that belong to the whole conversation.
GROUP_SUBJECT = "_group_"
CANDIDATE_CONFIDENCE = 0.6
REINFORCE_STEP = 0.15
ACCESS_CONFIDENCE_STEP = 0.02
CONTAINMENT_MIN_CHARS = 4

ACTIVE = "active"
CANDIDATE = "candidate"
ARCHIVED = "archived"
EXPIRED = "expired"
SUPERSEDED = "superseded"

VISIBILITIES = ("group", "personal")

# Punctuation/whitespace dropped before comparing two memory contents. Keeps
# "小明喜欢蓝莓蛋糕。" and "小明 喜欢蓝莓蛋糕" the same fact.
_IGNORED = set(
    " \t\r\n　!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    "，。！？、；：（）《》【】…—～·“”‘’"
)


def normalize_content(text: str) -> str:
    return "".join(ch for ch in text.strip().lower() if ch not in _IGNORED)


def dedupe_key(scope: str, subject_user_id: str, content: str) -> str:
    payload = f"{scope}\x1f{subject_user_id}\x1f{normalize_content(content)}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _encode_vector(vector) -> bytes:
    return array("f", [float(x) for x in vector]).tobytes()


def _decode_vector(blob) -> array | None:
    if not blob:
        return None
    try:
        values = array("f")
        values.frombytes(blob)
    except (ValueError, TypeError):
        return None
    return values or None


def _cosine(left, right) -> float | None:
    if left is None or right is None or len(left) != len(right) or not len(left):
        return None
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if not norm_left or not norm_right:
        return None
    return max(0.0, min(1.0, dot / (norm_left * norm_right)))


# Columns added after the original prototype schema. Additive only: an existing
# deployment keeps every row it already has.
_EXTRA_COLUMNS = (
    ("fact_type", "TEXT NOT NULL DEFAULT 'fact'"),
    ("confidence", "REAL NOT NULL DEFAULT 1.0"),
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("visibility", "TEXT NOT NULL DEFAULT 'group'"),
    ("origin_user_id", "TEXT"),
    ("expires_at", "INTEGER"),
    ("last_accessed_at", "INTEGER"),
    ("access_count", "INTEGER NOT NULL DEFAULT 0"),
    ("dedupe_key", "TEXT"),
    ("embedding", "BLOB"),
    ("embedding_model", "TEXT"),
    ("merged_into", "INTEGER"),
    ("updated_at", "INTEGER NOT NULL DEFAULT 0"),
    ("persona_summary", "TEXT NOT NULL DEFAULT ''"),
    ("mention_policy", "TEXT NOT NULL DEFAULT 'soft_echo'"),
)


def _existing_columns(db) -> set[str]:
    return {row[1] for row in db.execute("PRAGMA table_info(memories)")}


def _add_missing_columns(db) -> None:
    present = _existing_columns(db)
    for name, definition in _EXTRA_COLUMNS:
        if name not in present:
            db.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")


def _backfill(db) -> None:
    """Give pre-existing rows a dedupe key and a sane lifecycle state."""
    # Legacy rows were never edited, so their last write is their creation.
    db.execute("UPDATE memories SET updated_at=created_at WHERE updated_at=0")
    rows = db.execute(
        "SELECT id,scope,user_id,content,created_at FROM memories "
        "WHERE dedupe_key IS NULL ORDER BY id"
    ).fetchall()
    seen: dict[str, int] = {
        row[0]: row[1]
        for row in db.execute(
            "SELECT dedupe_key,id FROM memories WHERE dedupe_key IS NOT NULL"
        ).fetchall()
    }
    for row in rows:
        key = dedupe_key(row[1], row[2], row[3])
        survivor = seen.get(key)
        if survivor is None:
            seen[key] = row[0]
            db.execute(
                "UPDATE memories SET dedupe_key=? WHERE id=?", (key, row[0])
            )
        else:
            # Near-identical legacy row: keep the older one, retire the copy.
            db.execute(
                "UPDATE memories SET status=?, merged_into=?, updated_at=? WHERE id=?",
                (SUPERSEDED, survivor, row[4], row[0]),
            )


def _ensure_fts(db) -> None:
    """External-content FTS index. Prefers the trigram tokenizer so that
    unsegmented Chinese matches by substring; falls back on old SQLite."""
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    existing = (row[0] or "") if row else ""
    if "trigram" in existing:
        return
    if existing:
        db.execute("DROP TABLE memories_fts")
    created = False
    for tokenizer in ("trigram", None):
        option = ", tokenize='trigram'" if tokenizer else ""
        try:
            db.execute(
                "CREATE VIRTUAL TABLE memories_fts USING fts5("
                "content, content='memories', content_rowid='id'" + option + ")"
            )
            created = True
            break
        except sqlite3.OperationalError as exc:  # pragma: no cover - old sqlite
            log.warning("FTS5 %s tokenizer unavailable: %s", tokenizer or "default", exc)
    if created:
        db.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")


_TRIGGERS = """
    CREATE TRIGGER IF NOT EXISTS memory_insert AFTER INSERT ON memories BEGIN
      INSERT INTO memories_fts(rowid,content) VALUES(new.id,new.content);
    END;
    CREATE TRIGGER IF NOT EXISTS memory_delete AFTER DELETE ON memories BEGIN
      INSERT INTO memories_fts(memories_fts,rowid,content)
      VALUES('delete',old.id,old.content);
    END;
    CREATE TRIGGER IF NOT EXISTS memory_update AFTER UPDATE OF content ON memories BEGIN
      INSERT INTO memories_fts(memories_fts,rowid,content)
      VALUES('delete',old.id,old.content);
      INSERT INTO memories_fts(rowid,content) VALUES(new.id,new.content);
    END;
"""

_INDEXES = """
    CREATE INDEX IF NOT EXISTS memories_scope_status
      ON memories(scope, status, created_at DESC);
    CREATE INDEX IF NOT EXISTS memories_subject
      ON memories(scope, user_id, status);
    CREATE UNIQUE INDEX IF NOT EXISTS memories_dedupe
      ON memories(dedupe_key) WHERE dedupe_key IS NOT NULL;
"""


def migrate(db) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS memories (
          id INTEGER PRIMARY KEY, scope TEXT NOT NULL, user_id TEXT NOT NULL,
          content TEXT NOT NULL, importance INTEGER NOT NULL DEFAULT 1,
          source_event_id TEXT, created_at INTEGER NOT NULL
        );
        """
    )
    _add_missing_columns(db)
    _ensure_fts(db)
    db.executescript(_TRIGGERS)
    _backfill(db)
    db.executescript(_INDEXES)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_extraction_state (
          scope TEXT PRIMARY KEY,
          last_message_id INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS memory_extraction_runs (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          start_message_id INTEGER NOT NULL,
          end_message_id INTEGER NOT NULL,
          message_count INTEGER NOT NULL,
          strategy_version TEXT NOT NULL,
          model_name TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          raw_result TEXT NOT NULL DEFAULT '',
          accepted_count INTEGER NOT NULL DEFAULT 0,
          rejected_count INTEGER NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '',
          started_at INTEGER NOT NULL,
          finished_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS memory_extraction_runs_scope
          ON memory_extraction_runs(scope,id DESC);

        CREATE TABLE IF NOT EXISTS memory_evidence (
          id INTEGER PRIMARY KEY,
          memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
          event_id TEXT NOT NULL,
          user_id TEXT NOT NULL DEFAULT '',
          excerpt TEXT NOT NULL DEFAULT '',
          observed_at INTEGER NOT NULL DEFAULT 0,
          weight REAL NOT NULL DEFAULT 1.0,
          created_at INTEGER NOT NULL,
          UNIQUE(memory_id,event_id)
        );
        CREATE INDEX IF NOT EXISTS memory_evidence_memory
          ON memory_evidence(memory_id,id);

        CREATE TABLE IF NOT EXISTS memory_revisions (
          id INTEGER PRIMARY KEY,
          memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
          action TEXT NOT NULL,
          old_content TEXT NOT NULL DEFAULT '',
          new_content TEXT NOT NULL DEFAULT '',
          reason TEXT NOT NULL DEFAULT '',
          source_event_id TEXT,
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS memory_revisions_memory
          ON memory_revisions(memory_id,id DESC);

        CREATE TABLE IF NOT EXISTS memory_topics (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          canonical_name TEXT NOT NULL,
          summary TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'active',
          confidence REAL NOT NULL DEFAULT 0.5,
          first_seen_at INTEGER NOT NULL,
          last_seen_at INTEGER NOT NULL,
          mention_count INTEGER NOT NULL DEFAULT 1,
          UNIQUE(scope,canonical_name)
        );
        CREATE INDEX IF NOT EXISTS memory_topics_scope_activity
          ON memory_topics(scope,last_seen_at DESC);
        CREATE TABLE IF NOT EXISTS memory_topic_links (
          memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
          topic_id INTEGER NOT NULL REFERENCES memory_topics(id) ON DELETE CASCADE,
          weight REAL NOT NULL DEFAULT 1.0,
          created_at INTEGER NOT NULL,
          PRIMARY KEY(memory_id,topic_id)
        );

        CREATE TABLE IF NOT EXISTS memory_proposals (
          id INTEGER PRIMARY KEY,
          run_id INTEGER REFERENCES memory_extraction_runs(id) ON DELETE SET NULL,
          scope TEXT NOT NULL,
          subject_user_id TEXT NOT NULL,
          content TEXT NOT NULL,
          fact_type TEXT NOT NULL,
          confidence REAL NOT NULL,
          importance INTEGER NOT NULL,
          topic TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          reason TEXT NOT NULL DEFAULT '',
          memory_id INTEGER REFERENCES memories(id) ON DELETE SET NULL,
          created_at INTEGER NOT NULL,
          decided_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS memory_proposals_scope
          ON memory_proposals(scope,id DESC);
        """
    )


class MemoryStore(SqliteRepository):
    """``memories`` table plus its lexical/vector indexes.

    The optional embedding provider is injected, never imported: when it is
    missing or broken every read falls back to lexical matching and every
    write still succeeds.
    """

    def __init__(self, database, embedder=None):
        super().__init__(database)
        self.embedder = embedder

    def attach_embedder(self, embedder) -> "MemoryStore":
        self.embedder = embedder
        return self

    # ---------------------------------------------------------- extraction audit

    def extraction_cursor(self, scope: str) -> int:
        row = self.db.execute(
            "SELECT last_message_id FROM memory_extraction_state WHERE scope=?",
            (scope,),
        ).fetchone()
        return int(row[0]) if row else 0

    def start_extraction(
        self, scope: str, start_message_id: int, end_message_id: int,
        message_count: int, *, strategy_version: str, model_name: str = "",
        now: int | None = None,
    ) -> int:
        now = int(now or time.time())
        cursor = self.db.execute(
            "INSERT INTO memory_extraction_runs(scope,start_message_id,end_message_id,"
            "message_count,strategy_version,model_name,status,started_at) "
            "VALUES(?,?,?,?,?,?,'running',?)",
            (scope, start_message_id, end_message_id, message_count,
             strategy_version, model_name, now),
        )
        return int(cursor.lastrowid)

    def finish_extraction(
        self, run_id: int, *, status: str, raw_result: str = "",
        accepted_count: int = 0, rejected_count: int = 0, error: str = "",
        advance_to: int | None = None, now: int | None = None,
    ) -> None:
        now = int(now or time.time())
        with self.transaction():
            row = self.db.execute(
                "SELECT scope FROM memory_extraction_runs WHERE id=?", (run_id,)
            ).fetchone()
            self.db.execute(
                "UPDATE memory_extraction_runs SET status=?,raw_result=?,"
                "accepted_count=?,rejected_count=?,error=?,finished_at=? WHERE id=?",
                (status, raw_result[:20000], accepted_count, rejected_count,
                 error[:1000], now, run_id),
            )
            if row is not None and advance_to is not None:
                self.db.execute(
                    "INSERT INTO memory_extraction_state(scope,last_message_id,updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                    "last_message_id=MAX(last_message_id,excluded.last_message_id),"
                    "updated_at=excluded.updated_at",
                    (row[0], int(advance_to), now),
                )

    def extraction_runs(self, scope: str | None = None, limit: int = 50) -> list[dict]:
        if scope:
            rows = self.db.execute(
                "SELECT * FROM memory_extraction_runs WHERE scope=? ORDER BY id DESC LIMIT ?",
                (scope, max(1, int(limit))),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM memory_extraction_runs ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_proposal(self, run_id: int | None, scope: str, proposal) -> int:
        now = int(time.time())
        cursor = self.db.execute(
            "INSERT INTO memory_proposals(run_id,scope,subject_user_id,content,"
            "fact_type,confidence,importance,topic,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?, 'pending',?)",
            (run_id, scope, proposal.subject_user_id, proposal.content,
             proposal.fact_type, proposal.confidence, proposal.importance,
             proposal.topic, now),
        )
        return int(cursor.lastrowid)

    def decide_proposal(
        self, proposal_id: int, status: str, reason: str,
        memory_id: int | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE memory_proposals SET status=?,reason=?,memory_id=?,decided_at=? WHERE id=?",
            (status, reason[:500], memory_id, int(time.time()), proposal_id),
        )

    # ---------------------------------------------------------- provenance/topics

    def attach_evidence(self, memory_id: int, evidence) -> int:
        """Attach source observations idempotently to one memory."""
        added = 0
        now = int(time.time())
        with self.transaction():
            for item in evidence:
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO memory_evidence(memory_id,event_id,user_id,"
                    "excerpt,observed_at,weight,created_at) VALUES(?,?,?,?,?,?,?)",
                    (int(memory_id), item.event_id, item.user_id,
                     item.excerpt[:500], int(item.observed_at),
                     max(0.0, min(1.0, float(item.weight))), now),
                )
                added += cursor.rowcount or 0
        return added

    def evidence(self, memory_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM memory_evidence WHERE memory_id=? ORDER BY observed_at,id",
            (int(memory_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_revision(
        self, memory_id: int, action: str, *, old_content: str = "",
        new_content: str = "", reason: str = "", source_event_id: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO memory_revisions(memory_id,action,old_content,new_content,"
            "reason,source_event_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (int(memory_id), action, old_content, new_content, reason[:500],
             source_event_id, int(time.time())),
        )

    def revisions(self, memory_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM memory_revisions WHERE memory_id=? ORDER BY id DESC",
            (int(memory_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_topic(
        self, scope: str, name: str, *, summary: str = "",
        confidence: float = 0.5, seen_at: int | None = None,
    ) -> int | None:
        name = " ".join(name.strip().split())[:100]
        if not name:
            return None
        now = int(seen_at or time.time())
        existing = self.db.execute(
            "SELECT summary FROM memory_topics WHERE scope=? AND canonical_name=?",
            (scope, name),
        ).fetchone()
        compiled = str(existing[0] or "") if existing else ""
        incoming = " ".join(summary.strip().split())[:300]
        if incoming and incoming not in compiled:
            parts = [part.strip() for part in compiled.split("；") if part.strip()]
            parts.append(incoming)
            compiled = "；".join(parts[-3:])[:1000]
        self.db.execute(
            "INSERT INTO memory_topics(scope,canonical_name,summary,confidence,"
            "first_seen_at,last_seen_at,mention_count) VALUES(?,?,?,?,?,?,1) "
            "ON CONFLICT(scope,canonical_name) DO UPDATE SET "
            "summary=CASE WHEN excluded.summary!='' THEN excluded.summary ELSE summary END,"
            "confidence=MAX(confidence,excluded.confidence),last_seen_at=MAX(last_seen_at,excluded.last_seen_at),"
            "mention_count=mention_count+1",
            (scope, name, compiled, max(0.0, min(1.0, confidence)), now, now),
        )
        row = self.db.execute(
            "SELECT id FROM memory_topics WHERE scope=? AND canonical_name=?",
            (scope, name),
        ).fetchone()
        return int(row[0]) if row else None

    def link_topic(self, memory_id: int, topic_id: int, weight: float = 1.0) -> None:
        self.db.execute(
            "INSERT INTO memory_topic_links(memory_id,topic_id,weight,created_at) "
            "VALUES(?,?,?,?) ON CONFLICT(memory_id,topic_id) DO UPDATE SET "
            "weight=MAX(weight,excluded.weight)",
            (int(memory_id), int(topic_id), max(0.0, min(1.0, weight)), int(time.time())),
        )

    def topics(self, scope: str | None = None, limit: int = 100) -> list[dict]:
        clause, params = ("WHERE t.scope=?", (scope,)) if scope else ("", ())
        rows = self.db.execute(
            "SELECT t.*,count(l.memory_id) AS memory_count FROM memory_topics t "
            "LEFT JOIN memory_topic_links l ON l.topic_id=t.id "
            f"{clause} GROUP BY t.id ORDER BY t.last_seen_at DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ write

    def observe(
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
        visibility: str = "group",
        expires_at: int | None = None,
        supersede: bool = False,
        persona_summary: str = "",
        mention_policy: str = "soft_echo",
        now: int | None = None,
    ) -> dict:
        """Idempotent write. Returns an outcome describing what happened.

        ``outcome`` is ``inserted``, ``reinforced`` (the same fact seen again,
        which raises confidence), or ``superseded`` (this fact retired older
        similar rows). Facts below ``CANDIDATE_CONFIDENCE`` are stored as
        ``candidate`` and are not retrieved until a repeat observation or an
        access promotes them.
        """
        content = content.strip()[:1000]
        if not content:
            raise ValueError("empty memory")
        if visibility not in VISIBILITIES:
            raise ValueError(f"unknown visibility: {visibility}")
        now = int(now or time.time())
        confidence = max(0.0, min(1.0, float(confidence)))
        mention_policy = (
            mention_policy if mention_policy in
            {"direct", "soft_echo", "tone_only", "avoid_unless_asked"}
            else "soft_echo"
        )
        status = ACTIVE if confidence >= CANDIDATE_CONFIDENCE else CANDIDATE
        key = dedupe_key(scope, subject_user_id, content)
        # Network call, deliberately outside the write transaction.
        embedding = self._embed(content)

        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM memories WHERE dedupe_key=?", (key,)
            ).fetchone()
            if row is not None:
                return self._reinforce_row(
                    row, confidence, status, now,
                    persona_summary=persona_summary,
                    mention_policy=mention_policy,
                )
            retired = 0
            if supersede:
                retired = self._retire_similar(scope, subject_user_id, content, now=now)
            cursor = self.db.execute(
                "INSERT INTO memories(scope,user_id,content,importance,source_event_id,"
                "created_at,fact_type,confidence,status,visibility,origin_user_id,"
                "expires_at,last_accessed_at,access_count,dedupe_key,embedding,"
                "embedding_model,updated_at,persona_summary,mention_policy) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,?,?,?,?,?,?)",
                (
                    scope,
                    subject_user_id,
                    content,
                    max(1, min(5, int(importance))),
                    source_event_id,
                    now,
                    fact_type[:32],
                    confidence,
                    status,
                    visibility,
                    origin_user_id,
                    expires_at,
                    key,
                    _encode_vector(embedding) if embedding else None,
                    self._embedding_name() if embedding else None,
                    now,
                    persona_summary.strip()[:120],
                    mention_policy,
                ),
            )
        return {
            "outcome": "superseded" if retired else "inserted",
            "memory_id": int(cursor.lastrowid),
            "confidence": confidence,
            "status": status,
            "retired": retired,
        }

    def remember(
        self,
        scope: str,
        user_id: str,
        content: str,
        *,
        importance: int = 1,
        source_event_id: str | None = None,
        **kwargs,
    ) -> int:
        """Port-compatible write used by the recall path and older callers."""
        outcome = self.observe(
            scope,
            user_id,
            content,
            importance=importance,
            source_event_id=source_event_id,
            **kwargs,
        )
        return int(outcome["memory_id"])

    def _reinforce_row(
        self, row, confidence: float, status: str, now: int, *,
        persona_summary: str = "", mention_policy: str = "soft_echo",
    ) -> dict:
        merged = min(1.0, max(float(row["confidence"]), confidence) + REINFORCE_STEP)
        new_status = ACTIVE if merged >= CANDIDATE_CONFIDENCE else status
        self.db.execute(
            "UPDATE memories SET confidence=?, status=?, updated_at=?, "
            "last_accessed_at=?, access_count=access_count+1,"
            "persona_summary=CASE WHEN ?!='' THEN ? ELSE persona_summary END,"
            "mention_policy=? WHERE id=?",
            (
                merged, new_status, now, now,
                persona_summary.strip(), persona_summary.strip()[:120],
                mention_policy, row["id"],
            ),
        )
        return {
            "outcome": "reinforced",
            "memory_id": int(row["id"]),
            "confidence": merged,
            "status": new_status,
            "retired": 0,
        }

    def _retire_similar(
        self, scope: str, subject_user_id: str, content: str, *, now: int
    ) -> int:
        from ..memory.dedupe import similarity

        retired = 0
        for row in self.similar_candidates(scope, subject_user_id):
            if similarity(row["content"], content) >= CANDIDATE_CONFIDENCE:
                self.db.execute(
                    "UPDATE memories SET status=?, updated_at=? WHERE id=?",
                    (SUPERSEDED, now, row["id"]),
                )
                retired += 1
        return retired

    def reinforce(self, memory_ids, now: int | None = None) -> int:
        """Access reinforcement: touching a memory raises its confidence."""
        ids = [int(i) for i in memory_ids]
        if not ids:
            return 0
        now = int(now or time.time())
        with self.transaction():
            for memory_id in ids:
                self.db.execute(
                    "UPDATE memories SET access_count=access_count+1, "
                    "last_accessed_at=?, confidence=min(1.0, confidence+?), "
                    "status=CASE WHEN status=? THEN ? ELSE status END, updated_at=? "
                    "WHERE id=?",
                    (now, ACCESS_CONFIDENCE_STEP, CANDIDATE, ACTIVE, now, memory_id),
                )
        return len(ids)

    # ------------------------------------------------------------ read helpers

    def get(self, memory_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM memories WHERE id=?", (int(memory_id),)
        ).fetchone()
        return dict(row) if row else None

    def list_memories(
        self,
        scope: str | None = None,
        *,
        status: str | None = None,
        subject_user_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses, params = [], []
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        if status:
            clauses.append("status=?")
            params.append(status)
        if subject_user_id is not None:
            clauses.append("user_id=?")
            params.append(subject_user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(
            f"SELECT * FROM memories {where} ORDER BY id DESC LIMIT ?",
            (*params, max(1, int(limit))),
        ).fetchall()
        return [self._public(row) for row in rows]

    @staticmethod
    def _public(row) -> dict:
        entry = dict(row)
        entry.pop("embedding", None)
        return entry

    def similar_candidates(
        self, scope: str, subject_user_id: str, *, limit: int = 60
    ) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM memories WHERE scope=? AND user_id=? AND status IN (?,?) "
            "ORDER BY id DESC LIMIT ?",
            (scope, subject_user_id, ACTIVE, CANDIDATE, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def has_memory(self, scope: str, user_id: str, content: str) -> bool:
        target = normalize_content(content)
        if not target:
            return False
        row = self.db.execute(
            "SELECT 1 FROM memories WHERE scope=? AND user_id=? AND dedupe_key=? LIMIT 1",
            (scope, user_id, dedupe_key(scope, user_id, content)),
        ).fetchone()
        if row is not None:
            return True
        if len(target) < CONTAINMENT_MIN_CHARS:
            return False
        for candidate in self.similar_candidates(scope, user_id):
            existing = normalize_content(candidate["content"])
            if len(existing) >= CONTAINMENT_MIN_CHARS and (
                target in existing or existing in target
            ):
                return True
        return False

    # ---------------------------------------------------------------- retrieve

    def candidates(
        self,
        scope: str,
        query: str,
        *,
        subject_user_id: str | None = None,
        include_candidates: bool = False,
        limit: int = 48,
        now: int | None = None,
    ) -> list[dict]:
        """Raw retrieval: rows that pass the hard filters, each annotated with
        the signals ``lexical`` (bm25, lower is better), ``vector`` (cosine,
        0..1 or None) and ``match``. Ordering is the caller's job."""
        now = int(now or time.time())
        rows: dict[int, dict] = {}
        for row in self._lexical_rows(scope, query, subject_user_id, include_candidates, now, limit):
            rows[row["id"]] = row
        for row in self._vector_rows(scope, query, subject_user_id, include_candidates, now, limit):
            existing = rows.get(row["id"])
            if existing is not None:
                existing["vector"] = row["vector"]
            else:
                rows[row["id"]] = row
        for row in self._topic_rows(scope, query, subject_user_id, include_candidates, now, limit):
            rows.setdefault(row["id"], row)
        for row in rows.values():
            # Raw blobs are not JSON serialisable and the recall tool dumps rows.
            row.pop("embedding", None)
        return list(rows.values())

    def _topic_rows(
        self, scope: str, query: str, subject_user_id: str | None,
        include_candidates: bool, now: int, limit: int,
    ) -> list[dict]:
        tokens = self._tokens(query)
        if not tokens:
            return []
        where, params = self._filters(scope, subject_user_id, include_candidates, now)
        matched = " OR ".join("t.canonical_name LIKE ? OR t.summary LIKE ?" for _ in tokens)
        patterns = [value for token in tokens for value in (f"%{token}%", f"%{token}%")]
        rows = self.db.execute(
            "SELECT m.*,NULL AS lexical,NULL AS vector,'topic' AS match "
            "FROM memory_topics t JOIN memory_topic_links l ON l.topic_id=t.id "
            "JOIN memories m ON m.id=l.memory_id "
            f"WHERE t.scope=? AND ({matched}) AND {where} "
            "ORDER BY l.weight DESC,t.last_seen_at DESC LIMIT ?",
            (scope, *patterns, *params, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def search_memories(
        self, scope: str, query: str, limit: int = 5, **kwargs
    ) -> list[dict]:
        """Port-compatible search. Deterministic ordering (importance, recency);
        the agent's injection path uses ``MemoryService.recall`` instead."""
        rows = self.candidates(scope, query, limit=max(limit, 1) * 4, **kwargs)
        rows.sort(key=lambda row: (-row["importance"], -row["created_at"]))
        return rows[: max(1, int(limit))]

    def _filters(
        self, scope: str, subject_user_id: str | None, include_candidates: bool, now: int
    ) -> tuple[str, list]:
        clauses = [
            "m.scope=?",
            "m.status IN (?,?)" if include_candidates else "m.status=?",
            "(m.expires_at IS NULL OR m.expires_at>?)",
            "(m.visibility='group' OR m.user_id=?)",
        ]
        params: list = [scope]
        if include_candidates:
            params += [ACTIVE, CANDIDATE]
        else:
            params.append(ACTIVE)
        params += [now, subject_user_id if subject_user_id else "\x00nobody"]
        return " AND ".join(clauses), params

    @staticmethod
    def _terms(query: str) -> list[str]:
        return [word for word in query.replace('"', " ").split() if word]

    @classmethod
    def _fts_terms(cls, query: str) -> list[str]:
        """Trigram-searchable terms.

        A whole Chinese sentence is not one matchable phrase, so CJK runs
        longer than five characters are OR-ed as 3-character windows and ranked
        by how many hit. Latin words and short CJK phrases stay intact.
        """
        exact: list[str] = []
        windows: list[str] = []
        for word in cls._terms(query):
            cleaned = normalize_content(word)
            if len(cleaned) < 3:
                continue  # too short for trigram; the LIKE path handles it
            if len(cleaned) > 5 and not word.isascii():
                candidates = [
                    cleaned[start : start + 3] for start in range(len(cleaned) - 2)
                ]
                if len(candidates) > 8:
                    # Both ends: chat topics rarely sit in the middle only.
                    candidates = candidates[:4] + candidates[-4:]
                windows.extend(candidates)
            else:
                exact.append(cleaned[:40])
        terms = exact[:4]
        for window in windows:
            if len(terms) >= 8:
                break
            terms.append(window)
        unique: list[str] = []
        for term in terms:
            if term not in unique:
                unique.append(term)
        return unique[:8]

    def _lexical_rows(
        self,
        scope: str,
        query: str,
        subject_user_id: str | None,
        include_candidates: bool,
        now: int,
        limit: int,
    ) -> list[dict]:
        if not query.strip():
            return []
        where, params = self._filters(scope, subject_user_id, include_candidates, now)
        terms = self._fts_terms(query)
        if terms:
            match = " OR ".join('"' + term.replace('"', "") + '"' for term in terms)
            try:
                rows = self.db.execute(
                    "SELECT m.*, bm25(memories_fts) AS lexical, 'fts' AS match "
                    "FROM memories_fts JOIN memories m ON m.id=memories_fts.rowid "
                    f"WHERE memories_fts MATCH ? AND {where} "
                    "ORDER BY lexical LIMIT ?",
                    (match, *params, max(1, int(limit))),
                ).fetchall()
            except sqlite3.OperationalError as exc:  # pragma: no cover - bad query
                log.debug("FTS query failed, falling back to LIKE: %s", exc)
                rows = []
            if rows:
                return [dict(row) for row in rows]
        # Substring fallback for what the trigram index cannot serve. Chinese
        # has no whitespace word boundaries, so a whole message arrives as one
        # "word" and a substring match on it finds nothing; and the trigram
        # index needs three characters, while 备考 / 考试 / 喜欢 are two. This
        # path therefore carries most real queries and has to stay permissive.
        # ``query_tokens`` supplies the character bigrams that actually overlap
        # a memory.
        #
        # OR, never AND. A distilled fact is one short sentence; requiring every
        # term of the *question* to appear inside it matches almost nothing —
        # "备考 怎么样" would demand a memory containing both 备考 and 怎么样.
        # That is why recall silently returned nothing for whole-sentence input.
        # Sharing one topic word is the signal, so rank by overlap count and let
        # the caller's own scoring decide from there.
        words = self._tokens(query) or [query.strip()[:80]]
        patterns = [f"%{word[:80]}%" for word in words]
        hits = " + ".join(
            "(CASE WHEN m.content LIKE ? THEN 1 ELSE 0 END)" for _ in words
        )
        matched = " OR ".join("m.content LIKE ?" for _ in words)
        rows = self.db.execute(
            f"SELECT m.*, 0.0 AS lexical, 'like' AS match, ({hits}) AS hits "
            f"FROM memories m WHERE ({matched}) AND {where} "
            "ORDER BY hits DESC, m.importance DESC, m.created_at DESC LIMIT ?",
            (*patterns, *patterns, *params, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _tokens(query: str) -> list[str]:
        """Substring probes for a query, longest-useful first.

        Whitespace words plus character bigrams, minus anything long enough to
        only ever match itself. Sorted for a deterministic plan (and therefore a
        stable query plan and stable result ordering).
        """
        tokens = {
            token for token in query_tokens(query) if 1 <= len(token) <= 4
        }
        return [token for token in sorted(tokens) if token][:8]

    def _vector_rows(
        self,
        scope: str,
        query: str,
        subject_user_id: str | None,
        include_candidates: bool,
        now: int,
        limit: int,
    ) -> list[dict]:
        if self.embedder is None or not query.strip():
            return []
        query_vector = self._embed(query)
        if not query_vector:
            return []
        where, params = self._filters(scope, subject_user_id, include_candidates, now)
        rows = self.db.execute(
            "SELECT m.*, NULL AS lexical, 'vector' AS match FROM memories m "
            f"WHERE m.embedding IS NOT NULL AND {where} "
            "ORDER BY m.importance DESC, m.created_at DESC LIMIT ?",
            (*params, max(1, int(limit) * 20)),
        ).fetchall()
        scored: list[dict] = []
        for row in rows:
            entry = dict(row)
            entry["vector"] = _cosine(_decode_vector(row["embedding"]), query_vector)
            if entry["vector"] is not None:
                scored.append(entry)
        scored.sort(key=lambda item: -item["vector"])
        return scored[: max(1, int(limit))]

    def _embed(self, text: str):
        if self.embedder is None:
            return None
        try:
            vectors = self.embedder.embed([text])
        except Exception as exc:  # never let a provider break a write or a reply
            log.warning("embedding provider failed, using lexical only: %s", exc)
            return None
        if not vectors:
            return None
        return list(vectors[0])

    def _embedding_name(self) -> str:
        return str(getattr(self.embedder, "name", "") or "unknown")

    def reindex_embeddings(self, scope: str | None = None, limit: int = 200) -> int:
        """Backfill vectors for rows written before a provider was configured."""
        if self.embedder is None:
            return 0
        clause = "AND scope=?" if scope else ""
        params = (scope,) if scope else ()
        rows = self.db.execute(
            "SELECT id,content FROM memories WHERE embedding IS NULL "
            f"AND status IN (?,?) {clause} ORDER BY id DESC LIMIT ?",
            (ACTIVE, CANDIDATE, *params, max(1, int(limit))),
        ).fetchall()
        updated = 0
        for row in rows:
            vector = self._embed(row["content"])
            if not vector:
                continue
            self.db.execute(
                "UPDATE memories SET embedding=?, embedding_model=? WHERE id=?",
                (_encode_vector(vector), self._embedding_name(), row["id"]),
            )
            updated += 1
        return updated

    # --------------------------------------------------------------- lifecycle

    def expire(self, now: int | None = None) -> int:
        now = int(now or time.time())
        cursor = self.db.execute(
            "UPDATE memories SET status=?, updated_at=? "
            "WHERE status=? AND expires_at IS NOT NULL AND expires_at<=?",
            (EXPIRED, now, ACTIVE, now),
        )
        return cursor.rowcount or 0

    def promote(self, now: int | None = None, threshold: float = CANDIDATE_CONFIDENCE) -> int:
        now = int(now or time.time())
        cursor = self.db.execute(
            "UPDATE memories SET status=?, updated_at=? WHERE status=? AND confidence>=?",
            (ACTIVE, now, CANDIDATE, threshold),
        )
        return cursor.rowcount or 0

    def archive(
        self,
        now: int | None = None,
        *,
        older_than_days: int = 120,
        max_importance: int = 1,
        limit: int = 200,
    ) -> int:
        now = int(now or time.time())
        cursor = self.db.execute(
            "UPDATE memories SET status=?, updated_at=? WHERE id IN ("
            "  SELECT id FROM memories WHERE status=? AND importance<=? "
            "  AND access_count=0 AND last_accessed_at IS NULL AND created_at<? "
            "  ORDER BY created_at LIMIT ?)",
            (
                ARCHIVED,
                now,
                ACTIVE,
                max_importance,
                now - older_than_days * 86400,
                max(1, int(limit)),
            ),
        )
        return cursor.rowcount or 0

    def purge(
        self, *, status: str = EXPIRED, older_than_days: int = 30, now: int | None = None
    ) -> int:
        now = int(now or time.time())
        cursor = self.db.execute(
            "DELETE FROM memories WHERE status=? AND updated_at<?",
            (status, now - older_than_days * 86400),
        )
        return cursor.rowcount or 0

    def maintain(self, now: int | None = None) -> dict:
        now = int(now or time.time())
        with self.transaction():
            return {
                "expired": self.expire(now),
                "promoted": self.promote(now),
                "archived": self.archive(now),
                "purged": self.purge(now=now),
            }

    # ----------------------------------------------------------------- forgetting

    def forget(self, memory_ids) -> int:
        """Hard delete: rows, FTS entries (trigger) and vectors go together."""
        ids = [int(i) for i in memory_ids]
        if not ids:
            return 0
        with self.transaction():
            placeholders = ",".join("?" for _ in ids)
            cursor = self.db.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})", tuple(ids)
            )
        return cursor.rowcount or 0

    def forget_subject(self, scope: str, user_id: str) -> int:
        with self.transaction():
            cursor = self.db.execute(
                "DELETE FROM memories WHERE scope=? AND user_id=?", (scope, user_id)
            )
        return cursor.rowcount or 0

    def forget_scope(self, scope: str) -> int:
        with self.transaction():
            cursor = self.db.execute("DELETE FROM memories WHERE scope=?", (scope,))
        return cursor.rowcount or 0

    def stats(self, scope: str | None = None) -> dict:
        clause, params = ("WHERE scope=?", (scope,)) if scope else ("", ())
        rows = self.db.execute(
            f"SELECT status, count(*) AS total FROM memories {clause} GROUP BY status",
            params,
        ).fetchall()
        counts = {row["status"]: row["total"] for row in rows}
        vectors = self.db.execute(
            "SELECT count(*) FROM memories "
            + (f"{clause} AND " if clause else "WHERE ")
            + "embedding IS NOT NULL",
            params,
        ).fetchone()[0]
        return {"by_status": counts, "embedded": int(vectors), "total": sum(counts.values())}
