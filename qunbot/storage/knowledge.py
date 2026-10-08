"""Entities and relations between them, per scope.

The third shape this package stores, beside facts and their evidence. A fact
answers "what do I know about 小明"; a triple answers "how does 小明 relate to
the project", which is a question no single row of facts can answer.

Discovered by ``storage/database.py`` through its ``migrate`` hook, so adding
this file was enough — ``database.py`` was not edited.

Two properties are deliberate:

* **Reinforcement, never replacement.** Seeing ``(小明, 参与, 项目)`` again
  increments its ``mentions``; it never overwrites a competing row. Two rows
  claiming different objects for the same subject and relation are both kept,
  and :meth:`KnowledgeStore.conflicts_for` surfaces the pair as something to
  look at. Picking a winner
  silently is the one thing this store will not do — the reference design this
  is modelled on destroyed the older triple on every contradiction, which is
  unrecoverable and leaves no trace that it happened.
* **Scoped like everything else.** A triple belongs to one group. The same two
  names in another group are different rows with different evidence.
"""

from __future__ import annotations

import time

from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS kg_entities (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          name TEXT NOT NULL,
          mentions INTEGER NOT NULL DEFAULT 1,
          first_seen INTEGER NOT NULL,
          last_seen INTEGER NOT NULL,
          UNIQUE(scope, name)
        );
        CREATE INDEX IF NOT EXISTS kg_entities_scope_recent
          ON kg_entities(scope, last_seen DESC);

        CREATE TABLE IF NOT EXISTS kg_triples (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          subject TEXT NOT NULL,
          relation TEXT NOT NULL,
          object TEXT NOT NULL,
          mentions INTEGER NOT NULL DEFAULT 1,
          first_seen INTEGER NOT NULL,
          last_seen INTEGER NOT NULL,
          UNIQUE(scope, subject, relation, object)
        );
        CREATE INDEX IF NOT EXISTS kg_triples_subject
          ON kg_triples(scope, subject);
        CREATE INDEX IF NOT EXISTS kg_triples_object
          ON kg_triples(scope, object);

        CREATE TABLE IF NOT EXISTS kg_passages (
          scope TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          PRIMARY KEY(scope, fingerprint)
        );

        CREATE TABLE IF NOT EXISTS kg_state (
          scope TEXT PRIMARY KEY,
          last_message_id INTEGER NOT NULL DEFAULT 0,
          updated_at INTEGER NOT NULL
        );
    """)


class KnowledgeStore(SqliteRepository):
    """Entities, triples and the watermark that keeps extraction incremental."""

    # ------------------------------------------------------------ write path

    def record(
        self,
        scope: str,
        entities: list[str],
        triples: list[tuple[str, str, str]],
        *,
        now: int | None = None,
    ) -> dict:
        """Fold one passage's extraction in. Returns what changed.

        A repeat sighting is evidence, so it increments rather than rewrites:
        the row keeps the time it was first seen, which is what makes
        ``conflicts_for`` able to say which of two claims came first.
        """
        now = int(time.time()) if now is None else int(now)
        fresh_entities = 0
        fresh_triples = 0
        with self.transaction():
            for name in entities:
                if self._find("kg_entities", scope, name=name) is None:
                    fresh_entities += 1
                self.db.execute(
                    "INSERT INTO kg_entities(scope,name,mentions,first_seen,last_seen)"
                    " VALUES(?,?,1,?,?)"
                    " ON CONFLICT(scope,name) DO UPDATE SET"
                    " mentions=mentions+1, last_seen=excluded.last_seen",
                    (scope, name, now, now),
                )
            for subject, relation, obj in triples:
                if (
                    self._find(
                        "kg_triples",
                        scope,
                        subject=subject,
                        relation=relation,
                        object=obj,
                    )
                    is None
                ):
                    fresh_triples += 1
                self.db.execute(
                    "INSERT INTO kg_triples(scope,subject,relation,object,mentions,"
                    "first_seen,last_seen) VALUES(?,?,?,?,1,?,?)"
                    " ON CONFLICT(scope,subject,relation,object) DO UPDATE SET"
                    " mentions=mentions+1, last_seen=excluded.last_seen",
                    (scope, subject, relation, obj, now, now),
                )
        return {
            "entities": len(entities),
            "triples": len(triples),
            "new_entities": fresh_entities,
            "new_triples": fresh_triples,
        }

    def _find(self, table: str, scope: str, **keys):
        columns = ("scope", *keys)
        clauses = " AND ".join(f"{name}=?" for name in columns)
        return self.db.execute(
            f"SELECT id FROM {table} WHERE {clauses}",
            (scope, *keys.values()),
        ).fetchone()

    # ------------------------------------------------------------- read path

    def entities(self, scope: str, limit: int = 50) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM kg_entities WHERE scope=?"
            " ORDER BY mentions DESC, last_seen DESC LIMIT ?",
            (scope, max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def triples(self, scope: str, limit: int = 100) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM kg_triples WHERE scope=?"
            " ORDER BY mentions DESC, last_seen DESC LIMIT ?",
            (scope, max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def triples_about(self, scope: str, names, limit: int = 12) -> list[dict]:
        """Every triple touching any of ``names``, either side.

        Both sides on purpose: the interesting relation is as often "who did
        this to 小明" as "what did 小明 do".
        """
        wanted = [str(name) for name in names if str(name).strip()]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        rows = self.db.execute(
            f"SELECT * FROM kg_triples WHERE scope=?"
            f" AND (subject IN ({marks}) OR object IN ({marks}))"
            " ORDER BY mentions DESC, last_seen DESC LIMIT ?",
            (scope, *wanted, *wanted, max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def conflicts_for(self, scope: str, limit: int = 20) -> list[dict]:
        """Rows that agree on subject and relation but name a different object.

        These are *candidates* for a contradiction, not proof of one: 「小明
        参与 项目」 and 「小明 参与 实验室」 share subject and relation and can
        perfectly well both be true. So this is a list to look at, not a
        verdict — which is exactly why nothing here resolves them.

        Reported, never resolved. Which of two things a group said is true is
        not a judgement a store gets to make on its own, and the row it would
        delete is not recoverable. A different *relation* over the same pair
        (`小明 喜欢 咖啡` / `小明 讨厌 咖啡`) is not reported at all: those are
        two independent claims, not rivals.
        """
        rows = self.db.execute(
            # "a" and "b" are the two objects in alphabetical order, *not*
            # chronological — so the pair is reported once rather than twice,
            # and each side keeps its own first_seen so a reader can still tell
            # which claim came first.
            "SELECT a.subject, a.relation, a.object AS object_a,"
            " b.object AS object_b, a.first_seen AS seen_a,"
            " b.first_seen AS seen_b, a.mentions AS mentions_a,"
            " b.mentions AS mentions_b"
            " FROM kg_triples a JOIN kg_triples b"
            "   ON a.scope=b.scope AND a.subject=b.subject AND a.relation=b.relation"
            "   AND a.object<b.object"
            " WHERE a.scope=?"
            " ORDER BY a.last_seen DESC, b.last_seen DESC LIMIT ?",
            (scope, max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self, scope: str) -> dict:
        entities = self.db.execute(
            "SELECT count(*) FROM kg_entities WHERE scope=?", (scope,)
        ).fetchone()[0]
        triples = self.db.execute(
            "SELECT count(*) FROM kg_triples WHERE scope=?", (scope,)
        ).fetchone()[0]
        return {"entities": int(entities), "triples": int(triples)}

    # ------------------------------------------------- passage dedupe ledger

    def passage_seen(self, scope: str, digest: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM kg_passages WHERE scope=? AND fingerprint=?",
            (scope, digest),
        ).fetchone()
        return row is not None

    def mark_passage(self, scope: str, digest: str, *, now: int | None = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO kg_passages(scope,fingerprint,created_at)"
            " VALUES(?,?,?)",
            (scope, digest, int(time.time()) if now is None else int(now)),
        )

    # ---------------------------------------------------- scan watermark

    def last_scan(self, scope: str) -> int:
        row = self.db.execute(
            "SELECT last_message_id FROM kg_state WHERE scope=?", (scope,)
        ).fetchone()
        return int(row["last_message_id"]) if row else 0

    def advance_scan(self, scope: str, message_id: int) -> None:
        with self.transaction():
            self.db.execute(
                "INSERT INTO kg_state(scope,last_message_id,updated_at) VALUES(?,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET"
                " last_message_id=MAX(last_message_id, excluded.last_message_id),"
                " updated_at=excluded.updated_at",
                (scope, int(message_id), int(time.time())),
            )

    # ------------------------------------------------------------- deletion

    def forget_scope(self, scope: str) -> int:
        """Erase everything this store holds for one group."""
        removed = 0
        with self.transaction():
            for table in ("kg_triples", "kg_entities", "kg_passages", "kg_state"):
                cursor = self.db.execute(f"DELETE FROM {table} WHERE scope=?", (scope,))
                removed += cursor.rowcount
        return removed
