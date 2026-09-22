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
        status = ACTIVE if confidence >= CANDIDATE_CONFIDENCE else CANDIDATE
        key = dedupe_key(scope, subject_user_id, content)
        # Network call, deliberately outside the write transaction.
        embedding = self._embed(content)

        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM memories WHERE dedupe_key=?", (key,)
            ).fetchone()
            if row is not None:
                return self._reinforce_row(row, confidence, status, now)
            retired = 0
            if supersede:
                retired = self._retire_similar(scope, subject_user_id, content, now=now)
            cursor = self.db.execute(
                "INSERT INTO memories(scope,user_id,content,importance,source_event_id,"
                "created_at,fact_type,confidence,status,visibility,origin_user_id,"
                "expires_at,last_accessed_at,access_count,dedupe_key,embedding,"
                "embedding_model,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,?,?,?,?)",
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

    def _reinforce_row(self, row, confidence: float, status: str, now: int) -> dict:
        merged = min(1.0, max(float(row["confidence"]), confidence) + REINFORCE_STEP)
        new_status = ACTIVE if merged >= CANDIDATE_CONFIDENCE else status
        self.db.execute(
            "UPDATE memories SET confidence=?, status=?, updated_at=?, "
            "last_accessed_at=?, access_count=access_count+1 WHERE id=?",
            (merged, new_status, now, now, row["id"]),
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
        for row in rows.values():
            # Raw blobs are not JSON serialisable and the recall tool dumps rows.
            row.pop("embedding", None)
        return list(rows.values())

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
        # FTS tokenization may not match unsegmented Chinese or short queries.
        words = self._terms(query)[:5] or [query.strip()[:80]]
        like = " AND ".join("m.content LIKE ?" for _ in words)
        rows = self.db.execute(
            "SELECT m.*, 0.0 AS lexical, 'like' AS match FROM memories m "
            f"WHERE {like} AND {where} ORDER BY m.importance DESC, m.created_at DESC LIMIT ?",
            (*[f"%{word[:80]}%" for word in words], *params, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

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
