"""Group slang candidates: the one table group this feature owns.

Discovered by ``storage/database.py`` through its ``migrate`` hook, so adding
this file was enough — ``database.py`` was not edited.

Ownership boundary: this module reads the ``messages`` table (read-only, and
only because an extension has no handle on ``ConversationService``) and writes
only its own three tables. It never touches ``relationships``, ``memories``,
``jobs`` or any persona file. Affection belongs to relationships, memory
belongs to memory; this package stores none of either.
"""

from __future__ import annotations

import json
import time

from .base import SqliteRepository

# The only three states a term can be in. ``candidate`` is the default: seen
# often enough to be worth keeping, not yet trusted enough to use.
STATUS_CANDIDATE = "candidate"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_CANDIDATE, STATUS_APPROVED, STATUS_REJECTED)

# Where meaning inference is staged. Index i means "a meaning has been inferred
# once, when the term's count had reached thresholds[i]". A term is re-examined
# as it earns more evidence rather than being judged on its first sighting, and
# ``len(thresholds)`` means "retired from inference" (it was judged an ordinary
# word, or it has been through every stage).
INFERENCE_THRESHOLDS = (3, 6, 10, 20, 40, 60, 100)

# Meaning columns, added to `slang_candidates` after the fact. Kept additive on
# purpose: the WebUI reads this table with `SELECT *` and the browser reads
# named fields, so new columns ride along and a rename would break them.
_MEANING_COLUMNS = (
    ("meaning", "TEXT NOT NULL DEFAULT ''"),
    ("meaning_source", "TEXT NOT NULL DEFAULT ''"),  # '' | 'llm' | 'human'
    ("context_meaning", "TEXT NOT NULL DEFAULT ''"),
    ("standalone_meaning", "TEXT NOT NULL DEFAULT ''"),
    ("inference_stage", "INTEGER NOT NULL DEFAULT 0"),
    ("meaning_updated_at", "INTEGER NOT NULL DEFAULT 0"),
)

# A relative confidence change below this is not worth a write. Without it a
# scan would rewrite every candidate row to shave a millionth off each one.
_DECAY_EPSILON = 0.01

# A stored meaning is a sentence, not an essay: it is shown next to its term.
MAX_STORED_MEANING = 200


def next_threshold(thresholds, stage: int) -> int:
    """The occurrence count that buys the next inference stage.

    ``stage`` is an index into ``thresholds``; the last stage is the ceiling
    (a term that has been through them all is retired from inference).
    """
    if not thresholds:
        return 0
    index = max(0, min(int(stage), len(thresholds) - 1))
    return int(thresholds[index])


def stage_for(occurrences: int, thresholds) -> int:
    """The stage a term has earned, as the count of thresholds it has passed.

    Derived from the count rather than incremented, so a term that jumped
    straight from three uses to forty does not have to be inferred once per
    threshold it skipped. Reaching ``len(thresholds)`` retires it.
    """
    return sum(1 for threshold in thresholds if int(occurrences) >= int(threshold))


def _ensure_columns(db, table: str, columns) -> None:
    """Add the columns an older database is missing. No-op on a fresh one.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so the presence check is ours.
    """
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS slang_candidates (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          term TEXT NOT NULL,
          occurrences INTEGER NOT NULL DEFAULT 0,
          seen_users TEXT NOT NULL DEFAULT '[]',
          seen_days TEXT NOT NULL DEFAULT '[]',
          first_seen INTEGER NOT NULL,
          last_seen INTEGER NOT NULL,
          samples TEXT NOT NULL DEFAULT '[]',
          confidence REAL NOT NULL DEFAULT 0,
          meaning TEXT NOT NULL DEFAULT '',
          meaning_source TEXT NOT NULL DEFAULT '',
          context_meaning TEXT NOT NULL DEFAULT '',
          standalone_meaning TEXT NOT NULL DEFAULT '',
          inference_stage INTEGER NOT NULL DEFAULT 0,
          meaning_updated_at INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'candidate',
          origin TEXT NOT NULL DEFAULT 'auto',
          created_at INTEGER NOT NULL,
          updated_at INTEGER NOT NULL,
          UNIQUE(scope, term)
        );
        CREATE INDEX IF NOT EXISTS slang_candidates_scope_confidence
          ON slang_candidates(scope, confidence DESC);

        CREATE TABLE IF NOT EXISTS slang_reviews (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          term TEXT NOT NULL,
          action TEXT NOT NULL,
          note TEXT NOT NULL DEFAULT '',
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS slang_reviews_lookup
          ON slang_reviews(scope, term, created_at DESC);

        CREATE TABLE IF NOT EXISTS slang_scan_state (
          scope TEXT PRIMARY KEY,
          last_message_id INTEGER NOT NULL DEFAULT 0,
          last_gloss_at INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL
        );
    """)
    # Databases created before meaning inference existed.
    _ensure_columns(db, "slang_candidates", _MEANING_COLUMNS)
    _ensure_columns(db, "slang_scan_state", (("last_gloss_at", "INTEGER NOT NULL DEFAULT 0"),))
    db.execute(
        "CREATE INDEX IF NOT EXISTS slang_candidates_scope_stage"
        " ON slang_candidates(scope, inference_stage, occurrences DESC)"
    )


def _dumps(values: list) -> str:
    return json.dumps(list(values), ensure_ascii=False)


class SlangStore(SqliteRepository):
    """Candidate evidence and review state, scoped per group."""

    def recent_messages(self, scope: str, limit: int = 200) -> list[dict]:
        """Read-only mirror of the conversation log, oldest first.

        Present so the package works with zero wiring. The intended path is
        ``ConversationService.conversations.recent``; see
        ``qunbot.extensions.slang.bind_service``.
        """
        rows = self.db.execute(
            "SELECT * FROM messages WHERE scope=? ORDER BY id DESC LIMIT ?",
            (scope, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # -- candidates ---------------------------------------------------------

    def candidates(self, scope: str) -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT * FROM slang_candidates WHERE scope=?", (scope,)
        ).fetchall()
        return {row["term"]: dict(row) for row in rows}

    def top(self, scope: str, limit: int = 48) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM slang_candidates WHERE scope=? AND status!=?"
            " ORDER BY confidence DESC, occurrences DESC, term ASC LIMIT ?",
            (scope, STATUS_REJECTED, max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def promotable(
        self,
        scope: str,
        *,
        thresholds,
        limit: int,
        skip_human: bool = True,
    ) -> list[dict]:
        """Candidates that have earned their next round of meaning inference.

        A term is re-examined as it earns more evidence rather than being
        judged once on its first sighting, and a term already judged an
        ordinary word is retired by its stage sitting past the last threshold.
        """
        rows = self.db.execute(
            "SELECT * FROM slang_candidates WHERE scope=? AND status!=?"
            " AND inference_stage<?"
            " ORDER BY occurrences DESC, confidence DESC, term ASC",
            (scope, STATUS_REJECTED, len(thresholds)),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            if skip_human and row["meaning_source"] == "human":
                continue
            needed = next_threshold(thresholds, int(row["inference_stage"]))
            if int(row["occurrences"]) < needed:
                continue
            out.append(dict(row))
            if len(out) >= max(0, int(limit)):
                break
        return out

    def upsert(
        self,
        scope: str,
        term: str,
        *,
        occurrences: int,
        seen_users: list,
        seen_days: list,
        samples: list,
        confidence: float,
        now: int,
    ) -> None:
        """Write absolute counters, not increments.

        The caller merges with what is already stored, so a replayed window
        cannot double-count.
        """
        now = int(now)
        with self.transaction():
            self.db.execute(
                "INSERT INTO slang_candidates("
                " scope, term, occurrences, seen_users, seen_days, first_seen,"
                " last_seen, samples, confidence, status, origin,"
                " created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,'candidate','auto',?,?)"
                " ON CONFLICT(scope, term) DO UPDATE SET"
                " occurrences=excluded.occurrences,"
                " seen_users=excluded.seen_users,"
                " seen_days=excluded.seen_days,"
                " last_seen=excluded.last_seen,"
                " samples=excluded.samples,"
                " confidence=excluded.confidence,"
                " updated_at=excluded.updated_at",
                (
                    scope,
                    term,
                    int(occurrences),
                    _dumps(seen_users),
                    _dumps(seen_days),
                    now,
                    now,
                    _dumps(samples),
                    float(confidence),
                    now,
                    now,
                ),
            )

    def set_status(
        self, scope: str, term: str, status: str, *, origin: str = "review"
    ) -> bool:
        """Move a term between candidate/approved/rejected. Returns changed."""
        if status not in STATUSES:
            raise ValueError(f"unknown slang status: {status}")
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE slang_candidates SET status=?, origin=?, updated_at=?"
                " WHERE scope=? AND term=? AND status!=?",
                (status, origin, int(time.time()), scope, term, status),
            )
        return cursor.rowcount > 0

    def set_meaning(
        self,
        scope: str,
        term: str,
        meaning: str,
        *,
        source: str,
        stage: int,
        now: int,
        context_meaning: str = "",
        standalone_meaning: str = "",
        force: bool = False,
    ) -> bool:
        """Record a meaning. An automatic write never replaces a human one.

        The guard is in the SQL, not in the caller, so no future call site can
        forget it. A deployer who corrected a definition outranks the model,
        and re-learning must not quietly undo that — the same reason `upsert`
        leaves the meaning columns out of its conflict clause.
        """
        if source not in ("llm", "human"):
            raise ValueError(f"unknown meaning source: {source}")
        now = int(now)
        guard = "" if (force or source == "human") else " AND meaning_source!='human'"
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE slang_candidates SET meaning=?, meaning_source=?,"
                " context_meaning=?, standalone_meaning=?, inference_stage=?,"
                " meaning_updated_at=?, updated_at=?"
                f" WHERE scope=? AND term=?{guard}",
                (
                    str(meaning)[:MAX_STORED_MEANING],
                    source,
                    str(context_meaning)[:MAX_STORED_MEANING],
                    str(standalone_meaning)[:MAX_STORED_MEANING],
                    int(stage),
                    now,
                    now,
                    scope,
                    term,
                ),
            )
        return cursor.rowcount > 0

    def clear_human_meaning(self, scope: str, term: str, *, now: int) -> bool:
        """Hand a term back to automatic inference, stage reset to the start."""
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE slang_candidates SET meaning_source='', inference_stage=0,"
                " meaning_updated_at=?, updated_at=?"
                " WHERE scope=? AND term=? AND meaning_source='human'",
                (int(now), int(now), scope, term),
            )
        return cursor.rowcount > 0

    def log_review(
        self, scope: str, term: str, action: str, note: str = ""
    ) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO slang_reviews(scope, term, action, note, created_at)"
                " VALUES(?,?,?,?,?)",
                (scope, term, action, note[:200], int(time.time())),
            )

    def reviews(self, scope: str | None = None, limit: int = 100) -> list[dict]:
        if scope is None:
            rows = self.db.execute(
                "SELECT * FROM slang_reviews ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM slang_reviews WHERE scope=? ORDER BY id DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, scope: str, keep: int) -> int:
        """Drop the least-supported unreviewed candidates. Reviewed rows stay.

        A deployer's decision is not eviction material: pruning silently
        dropping an approval would look like a bug in the review file.
        """
        keep = max(0, int(keep))
        with self.transaction():
            cursor = self.db.execute(
                "DELETE FROM slang_candidates WHERE scope=? AND status=? AND id NOT IN"
                " (SELECT id FROM slang_candidates WHERE scope=? AND status=?"
                "  ORDER BY confidence DESC, occurrences DESC, term ASC LIMIT ?)",
                (scope, STATUS_CANDIDATE, scope, STATUS_CANDIDATE, keep),
            )
        return cursor.rowcount

    def decay(self, scope: str, *, now: int, half_life: float) -> int:
        """Halve an unused candidate's confidence once per ``half_life``.

        A term is evidence about *now*: one that was briefly popular months ago
        should not keep a top confidence and hold a prompt slot against what the
        group is actually saying today. Without this, confidence only ever went
        up, and the top-N was decided by accumulated history rather than by
        current use.

        Only ``candidate`` rows decay. Reviewed rows are a deployer's decision,
        and `prune` already refuses to evict them for the same reason — quietly
        decaying an approval would look like the review file had stopped
        working.
        """
        if half_life <= 0:
            return 0
        now = int(now)
        rows = self.db.execute(
            "SELECT term, confidence, last_seen FROM slang_candidates"
            " WHERE scope=? AND status=? AND confidence>0",
            (scope, STATUS_CANDIDATE),
        ).fetchall()
        changed: list[tuple[float, int, str, str]] = []
        for row in rows:
            current = float(row["confidence"])
            elapsed = max(0, now - int(row["last_seen"]))
            decayed = current * (0.5 ** (elapsed / float(half_life)))
            if decayed < current * (1.0 - _DECAY_EPSILON):
                changed.append((decayed, now, scope, row["term"]))
        if not changed:
            return 0
        with self.transaction():
            self.db.executemany(
                "UPDATE slang_candidates SET confidence=?, updated_at=?"
                " WHERE scope=? AND term=?",
                changed,
            )
        return len(changed)

    # -- scan watermark -----------------------------------------------------

    def last_scan(self, scope: str) -> int:
        row = self.db.execute(
            "SELECT last_message_id FROM slang_scan_state WHERE scope=?", (scope,)
        ).fetchone()
        return int(row["last_message_id"]) if row else 0

    def advance_scan(self, scope: str, message_id: int) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO slang_scan_state(scope, last_message_id, updated_at)"
                " VALUES(?,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " last_message_id=MAX(last_message_id, excluded.last_message_id),"
                " updated_at=excluded.updated_at",
                (scope, int(message_id), int(time.time())),
            )

    # -- meaning-inference watermark ----------------------------------------

    def last_gloss(self, scope: str) -> int:
        """When inference last ran for this scope, persisted across restarts."""
        row = self.db.execute(
            "SELECT last_gloss_at FROM slang_scan_state WHERE scope=?", (scope,)
        ).fetchone()
        return int(row["last_gloss_at"]) if row else 0

    def mark_gloss(self, scope: str, now: int) -> None:
        """Record the attempt, successful or not.

        Marking on failure is the point: a model that is misconfigured or
        unreachable must cost one attempt per interval, not one attempt per
        scan. The next try waits out the interval like any other.
        """
        now = int(now)
        with self.transaction():
            self.db.execute(
                "INSERT INTO slang_scan_state(scope, last_message_id, last_gloss_at,"
                " updated_at) VALUES(?,0,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " last_gloss_at=excluded.last_gloss_at,"
                " updated_at=excluded.updated_at",
                (scope, now, now),
            )

    def forget(self, scope: str, term: str | None = None) -> int:
        """Delete learned data for a scope, or one term in it."""
        with self.transaction():
            if term is None:
                cursor = self.db.execute(
                    "DELETE FROM slang_candidates WHERE scope=?", (scope,)
                )
            else:
                cursor = self.db.execute(
                    "DELETE FROM slang_candidates WHERE scope=? AND term=?",
                    (scope, term),
                )
        return cursor.rowcount
