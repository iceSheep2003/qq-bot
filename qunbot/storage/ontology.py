"""Scoped SQLite graph adapter. Atomic, validated writes and revision history.

There is intentionally no schema/type registration API. Validation loads a
reference-closed affected projection, not the entire ever-growing group graph.
"""

from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy

from ..contracts.schema import ContractError, canonical
from ..ontology.objects import references
from ..ontology.v1 import validate_graph
from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
      CREATE TABLE IF NOT EXISTS ontology_nodes (
        scope TEXT NOT NULL, id TEXT NOT NULL, type TEXT NOT NULL,
        revision INTEGER NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(scope,id)
      );
      CREATE INDEX IF NOT EXISTS ontology_nodes_type ON ontology_nodes(scope,type,id);
      CREATE UNIQUE INDEX IF NOT EXISTS ontology_person_identity ON ontology_nodes(
        scope,json_extract(body,'$.platform'),json_extract(body,'$.account_ref')) WHERE type='Person';
      CREATE TABLE IF NOT EXISTS ontology_refs (
        scope TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
        PRIMARY KEY(scope,source,target),
        FOREIGN KEY(scope,source) REFERENCES ontology_nodes(scope,id),
        FOREIGN KEY(scope,target) REFERENCES ontology_nodes(scope,id)
      );
      CREATE INDEX IF NOT EXISTS ontology_refs_target ON ontology_refs(scope,target);
      CREATE TABLE IF NOT EXISTS ontology_edges (
        scope TEXT NOT NULL, id TEXT NOT NULL, relation TEXT NOT NULL,
        source TEXT NOT NULL, target TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY(scope,id), UNIQUE(scope,relation,source,target),
        FOREIGN KEY(scope,source) REFERENCES ontology_nodes(scope,id),
        FOREIGN KEY(scope,target) REFERENCES ontology_nodes(scope,id)
      );
      CREATE INDEX IF NOT EXISTS ontology_edges_source ON ontology_edges(scope,source);
      CREATE INDEX IF NOT EXISTS ontology_edges_target ON ontology_edges(scope,target);
      CREATE TABLE IF NOT EXISTS ontology_revisions (
        scope TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL,
        body TEXT NOT NULL, reason TEXT NOT NULL,
        PRIMARY KEY(scope,id,revision)
      );
      CREATE TABLE IF NOT EXISTS ontology_imports (
        source_hash TEXT NOT NULL, pipeline_version TEXT NOT NULL,
        row_offset INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
        PRIMARY KEY(source_hash,pipeline_version)
      );
      CREATE TABLE IF NOT EXISTS ontology_terms (
        scope TEXT NOT NULL, term TEXT NOT NULL, claim_id TEXT NOT NULL,
        PRIMARY KEY(scope,term,claim_id),
        FOREIGN KEY(scope,claim_id) REFERENCES ontology_nodes(scope,id)
      );
    """)


class GraphStore(SqliteRepository):
    def __init__(self, database):
        super().__init__(database)
        self.lock = database._transaction_lock

    def get(self, scope: str, identity: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT body FROM ontology_nodes WHERE scope=? AND id=?",
                (scope, identity),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def revision(self, scope: str, identity: str) -> int:
        with self.lock:
            row = self.db.execute(
                "SELECT revision FROM ontology_nodes WHERE scope=? AND id=?",
                (scope, identity),
            ).fetchone()
            return int(row[0]) if row else 0

    def list_nodes(self, scope: str, kind: str, *, limit=100, after="") -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM ontology_nodes WHERE scope=? AND type=? AND id>? ORDER BY id LIMIT ?",
                (scope, kind, after, max(1, min(1000, limit))),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def console_nodes(
        self,
        kind,
        *,
        scope=None,
        status=None,
        query="",
        limit=100,
        after="",
        concept_kind=None,
    ):
        """Bounded owner read model; filtering occurs before LIMIT, not in Python.

        Cursor is a (scope, id) pair encoded as JSON. It is never an executable
        expression, and scope remains an independent mandatory filter when set.
        """
        cursor = json.loads(after) if after else ["", ""]
        if (
            not isinstance(cursor, list)
            or len(cursor) != 2
            or not all(isinstance(v, str) for v in cursor)
        ):
            raise ValueError("Invalid graph cursor")
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM ontology_nodes WHERE type=? "
                "AND (? IS NULL OR scope=?) AND (scope,id)>(?,?) "
                "AND (? IS NULL OR COALESCE(json_extract(body,'$.epistemic_status'),json_extract(body,'$.status'))=?) "
                "AND (? IS NULL OR json_extract(body,'$.kind')=?) "
                "AND (?='' OR instr(lower(COALESCE(json_extract(body,'$.typed_value.value'),json_extract(body,'$.name'),json_extract(body,'$.definition'),json_extract(body,'$.account_ref'),'')),lower(?))>0) "
                "ORDER BY scope,id LIMIT ?",
                (
                    kind,
                    scope,
                    scope,
                    *cursor,
                    status,
                    status,
                    concept_kind,
                    concept_kind,
                    query[:500],
                    query[:500],
                    max(1, min(200, limit)),
                ),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]

    def console_counts(self, scope=None):
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT scope,type,json_extract(body,'$.kind') AS kind,"
                    "COALESCE(json_extract(body,'$.epistemic_status'),json_extract(body,'$.status')) AS status,count(*) AS count "
                    "FROM ontology_nodes WHERE (? IS NULL OR scope=?) GROUP BY scope,type,kind,status",
                    (scope, scope),
                )
            ]

    def incoming(self, scope, identity, relation=None, *, limit=100):
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM ontology_edges WHERE scope=? AND target=? AND (? IS NULL OR relation=?) ORDER BY id LIMIT ?",
                    (scope, identity, relation, relation, max(1, min(200, limit))),
                )
            ]

    def revision_history(self, scope, identity, *, limit=20):
        with self.lock:
            return [
                {"revision": row[0], "reason": row[1], "node": json.loads(row[2])}
                for row in self.db.execute(
                    "SELECT revision,reason,body FROM ontology_revisions WHERE scope=? AND id=? ORDER BY revision DESC LIMIT ?",
                    (scope, identity, max(1, min(100, limit))),
                )
            ]

    def write(
        self,
        scope: str,
        nodes: list[dict],
        edges: list[dict],
        *,
        reason: str,
        expected_revisions: dict[str, int] | None = None,
    ) -> int:
        """Validate closure including dependants of changed nodes before mutation.

        expected_revisions enables optimistic compare-and-swap for knowledge
        corrections and workflow updates. Identical replays create no revision.
        """
        if not nodes and not edges:
            return 0
        if len({n["id"] for n in nodes}) != len(nodes) or len(
            {e["id"] for e in edges}
        ) != len(edges):
            raise ContractError("Duplicate mutation identity")
        if not reason or len(reason) > 200:
            raise ContractError("A bounded revision reason is required")
        nodes, edges = deepcopy(nodes), deepcopy(edges)
        with self.transaction():
            for identity, expected in (expected_revisions or {}).items():
                if self.revision(scope, identity) != expected:
                    raise ContractError("Stale graph revision")
            merged = {n["id"]: n for n in nodes}
            changed = []
            for node in nodes:
                old = self.get(scope, node["id"])
                if old and old["type"] != node["type"]:
                    raise ContractError("Entity type is immutable")
                if (
                    old
                    and node["type"] == "Person"
                    and (old["platform"], old["account_ref"])
                    != (node["platform"], node["account_ref"])
                ):
                    raise ContractError("Platform identity is immutable")
                if old != node:
                    changed.append(node["id"])
            # Include existing reference dependants and touching edges of only
            # changed roots. Do not fan out from shared Group/Person dependencies.
            frontier = list(changed)
            seen = set(frontier)
            existing_edges = {}
            while frontier:
                identity = frontier.pop()
                for row in self.db.execute(
                    "SELECT source FROM ontology_refs WHERE scope=? AND target=?",
                    (scope, identity),
                ):
                    if row[0] not in seen:
                        seen.add(row[0])
                        frontier.append(row[0])
                for row in self.db.execute(
                    "SELECT body FROM ontology_edges WHERE scope=? AND (source=? OR target=?)",
                    (scope, identity, identity),
                ):
                    link = json.loads(row[0])
                    existing_edges[link["id"]] = link
                if len(seen) > 10000:
                    raise ContractError("Affected graph exceeds validation bounds")
            pending = (
                list(seen)
                + list(merged)
                + [r["id"] for e in edges for r in references(e)]
            )
            pending += [r["id"] for e in existing_edges.values() for r in references(e)]
            expanded = set()
            while pending:
                identity = pending.pop()
                if identity in expanded:
                    continue
                expanded.add(identity)
                node = merged.get(identity) or self.get(scope, identity)
                if node is None:
                    raise ContractError("Reference target is missing")
                merged[identity] = node
                pending.extend(r["id"] for r in references(node))
                # Accepted claims need their supporting edges even when loaded
                # only as a dependency of another claim.
                for row in self.db.execute(
                    "SELECT body FROM ontology_edges WHERE scope=? AND source=? AND relation='has_evidence'",
                    (scope, identity),
                ):
                    link = json.loads(row[0])
                    existing_edges[link["id"]] = link
                    pending.append(link["target_ref"]["id"])
                if len(expanded) > 10000 or len(existing_edges) + len(edges) > 20000:
                    raise ContractError("Affected graph exceeds validation bounds")
            existing_edges.update({e["id"]: e for e in edges})
            # Touching incoming edges may introduce another endpoint.
            for link in existing_edges.values():
                for r in references(link):
                    if r["id"] not in merged:
                        node = self.get(scope, r["id"])
                        if node is None:
                            raise ContractError("Missing edge endpoint")
                        merged[r["id"]] = node
            validate_graph(
                list(merged.values()), list(existing_edges.values()), scope=scope
            )
            for node in nodes:
                if node["id"] not in changed:
                    continue
                revision = self.revision(scope, node["id"]) + 1
                body = canonical(node).decode()
                self.db.execute(
                    "INSERT INTO ontology_nodes VALUES(?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET revision=excluded.revision,body=excluded.body",
                    (scope, node["id"], node["type"], revision, body),
                )
                self.db.execute(
                    "INSERT INTO ontology_revisions VALUES(?,?,?,?,?)",
                    (scope, node["id"], revision, body, reason),
                )
            # All targets now exist; foreign keys also enforce scope isolation.
            for node in nodes:
                if node["id"] not in changed:
                    continue
                self.db.execute(
                    "DELETE FROM ontology_refs WHERE scope=? AND source=?",
                    (scope, node["id"]),
                )
                for r in {r["id"]: r for r in references(node)}.values():
                    self.db.execute(
                        "INSERT INTO ontology_refs VALUES(?,?,?)",
                        (scope, node["id"], r["id"]),
                    )
                if node["type"] == "Claim":
                    predicate = merged[node["predicate_ref"]["id"]]
                    value = str((node["typed_value"] or {}).get("value", ""))
                    self.db.execute(
                        "DELETE FROM ontology_terms WHERE scope=? AND claim_id=?",
                        (scope, node["id"]),
                    )
                    self.db.executemany(
                        "INSERT INTO ontology_terms VALUES(?,?,?)",
                        [
                            (scope, t, node["id"])
                            for t in terms(predicate["name"] + " " + value)
                        ],
                    )
            for link in edges:
                prior = self.db.execute(
                    "SELECT relation,source,target FROM ontology_edges WHERE scope=? AND id=?",
                    (scope, link["id"]),
                ).fetchone()
                if prior and tuple(prior) != (
                    link["relation"],
                    link["source_ref"]["id"],
                    link["target_ref"]["id"],
                ):
                    raise ContractError("Edge identity is immutable")
                self.db.execute(
                    "INSERT INTO ontology_edges VALUES(?,?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET body=excluded.body",
                    (
                        scope,
                        link["id"],
                        link["relation"],
                        link["source_ref"]["id"],
                        link["target_ref"]["id"],
                        canonical(link).decode(),
                    ),
                )
            return len(changed)

    def search_claims(self, scope, query, *, limit=200, subject_id=None):
        tokens = sorted(terms(query))[:100]
        if not tokens:
            return []
        with self.lock:
            placeholders = ",".join("?" for _ in tokens)
            rows = self.db.execute(
                f"SELECT n.body,count(*) AS score FROM ontology_terms t JOIN ontology_nodes n ON n.scope=t.scope AND n.id=t.claim_id "
                f"WHERE t.scope=? AND t.term IN ({placeholders}) AND (? IS NULL OR json_extract(n.body,'$.subject_ref.id')=?) "
                "AND json_extract(n.body,'$.visibility')='group' GROUP BY n.id ORDER BY score DESC,n.id LIMIT ?",
                (scope, *tokens, subject_id, subject_id, max(1, min(1000, limit))),
            ).fetchall()
            return [(json.loads(r[0]), float(r[1]) / len(tokens)) for r in rows]

    def outgoing(
        self, scope: str, identity: str, relation: str | None = None
    ) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM ontology_edges WHERE scope=? AND source=? AND (? IS NULL OR relation=?) ORDER BY id LIMIT 1000",
                (scope, identity, relation, relation),
            ).fetchall()
            return [json.loads(r[0]) for r in rows]

    def forget_person(self, scope, account_ref):
        """Owner-only hard erasure of person and all reference dependants.

        This deliberately removes evidence-backed records and affected episode
        summaries, rather than leaving snippets or revision history behind.
        Kept out of the fixed model tool surface.
        """
        with self.transaction():
            root = self.db.execute(
                "SELECT id FROM ontology_nodes WHERE scope=? AND type='Person' AND json_extract(body,'$.account_ref')=?",
                (scope, account_ref),
            ).fetchone()
            if not root:
                return 0
            removed = {root[0]}
            pending = [root[0]]
            while pending:
                identity = pending.pop()
                for row in self.db.execute(
                    "SELECT source FROM ontology_refs WHERE scope=? AND target=?",
                    (scope, identity),
                ):
                    if row[0] not in removed:
                        removed.add(row[0])
                        pending.append(row[0])
            for identity in removed:
                self.db.execute(
                    "DELETE FROM ontology_edges WHERE scope=? AND (source=? OR target=?)",
                    (scope, identity, identity),
                )
                self.db.execute(
                    "DELETE FROM ontology_refs WHERE scope=? AND (source=? OR target=?)",
                    (scope, identity, identity),
                )
                self.db.execute(
                    "DELETE FROM ontology_terms WHERE scope=? AND claim_id=?",
                    (scope, identity),
                )
                self.db.execute(
                    "DELETE FROM ontology_revisions WHERE scope=? AND id=?",
                    (scope, identity),
                )
            for identity in removed:
                self.db.execute(
                    "DELETE FROM ontology_nodes WHERE scope=? AND id=?",
                    (scope, identity),
                )
            return len(removed)

    def stats(self) -> dict:
        with self.lock:
            return {
                "nodes": [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT scope,type,count(*) AS count FROM ontology_nodes GROUP BY scope,type"
                    )
                ],
                "edges": [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT scope,relation,count(*) AS count FROM ontology_edges GROUP BY scope,relation"
                    )
                ],
                "foreign_key_violations": len(
                    self.db.execute("PRAGMA foreign_key_check").fetchall()
                ),
            }

    def edge_page(self, scope, *, after="", limit=1000):
        with self.lock:
            rows = self.db.execute(
                "SELECT body FROM ontology_edges WHERE scope=? AND id>? ORDER BY id LIMIT ?",
                (scope, after, max(1, min(1000, limit))),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]


def terms(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", text).casefold()[:12000]
    tokens = set(re.findall(r"[a-z0-9_]{2,40}", folded))
    for run in re.findall(r"[\u3400-\u9fff]+", folded):
        tokens.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            tokens.add(run)
    return tokens
