"""Bounded owner graph view with explicit report status and source evidence."""

from __future__ import annotations

import json


class OntologyConsole:
    def __init__(self, knowledge):
        self.knowledge = knowledge
        self.graph = knowledge.graph

    def scopes(self):
        return sorted({row["scope"] for row in self.graph.console_counts()})

    def overview(self, scope=None):
        rows = self.graph.console_counts(scope)
        statuses = {}
        for row in rows:
            if row["type"] == "Claim":
                statuses[row["status"]] = statuses.get(row["status"], 0) + row["count"]
        return {
            "source": "ontology",
            "scope": scope or "",
            "scopes": self.scopes(),
            "memories": {
                "total": sum(statuses.values()),
                "by_status": statuses,
                "topics": sum(
                    r["count"]
                    for r in rows
                    if r["type"] == "Concept" and r["kind"] == "topic"
                ),
            },
            "people": sum(r["count"] for r in rows if r["type"] == "Person"),
            "slang": sum(r["count"] for r in rows if r["type"] == "SlangSense"),
        }

    def records(
        self,
        kind,
        scope=None,
        *,
        limit=100,
        after="",
        status=None,
        query="",
        concept_kind=None,
    ):
        return [
            self.record(node)
            for node in self.graph.console_nodes(
                kind,
                scope=scope,
                limit=limit,
                after=after,
                status=status,
                query=query,
                concept_kind=concept_kind,
            )
        ]

    def record(self, node):
        scope = node["scope"]
        kind = node["type"]
        label = (
            node.get("name")
            or node.get("definition")
            or node.get("account_ref")
            or kind
        )
        metadata = []
        status = node.get("epistemic_status") or node.get("status") or "observed"
        if kind == "Person":
            memberships = self.graph.incoming(scope, node["id"], "represents", limit=1)
            if memberships:
                member = self.graph.get(scope, memberships[0]["source_ref"]["id"])
                if member:
                    label = member["card"] or member["nickname"] or label
        if kind == "Claim":
            value = node["typed_value"]
            label = str(value["value"]) if value else node["object_ref"]["id"]
            predicate = self.graph.get(scope, node["predicate_ref"]["id"])
            metadata = [
                predicate["name"] if predicate else "关系说法",
                node["source_kind"],
                node["modality"],
            ]
        if kind == "SlangSense":
            term = self.graph.get(scope, node["term_ref"]["id"])
            label = term["name"] if term else "未知词语"
            status = term["status"] if term else "unknown"
            metadata = [
                "有释义证据",
                "来源已记录" if node["origin_evidence_refs"] else "起源未知",
            ]
        return {
            "ontology": True,
            "id": node["id"],
            "ref_id": node["id"],
            "scope": scope,
            "entity_type": kind,
            "label": label,
            "metadata": metadata,
            "summary": node.get("definition", ""),
            "status": status,
            "updated_at_ms": node["updated_at"],
            "cursor": json.dumps([scope, node["id"]], separators=(",", ":")),
        }

    def graph_view(self, scope, limit=120):
        if not scope:
            return {"nodes": [], "links": [], "schema_version": 1}
        limit = max(1, min(200, limit))
        nodes = {}
        links = []
        # Reserve half the budget for neighbours. Round-robin seeds prevent
        # a busy Claim table from crowding people and vocabulary out entirely.
        seed_budget = max(1, limit // 2)
        pools = []
        for kind in (
            "Claim",
            "SlangSense",
            "Concept",
            "Person",
            "AgentIdentity",
            "SocialState",
        ):
            pool = []
            for node in self.graph.list_nodes(scope, kind, limit=min(40, limit)):
                if kind == "Concept" and node["kind"] == "predicate":
                    continue
                pool.append(node)
            pools.append(pool)
        while any(pools) and len(nodes) < seed_budget:
            for pool in pools:
                if pool and len(nodes) < seed_budget:
                    node = pool.pop(0)
                    nodes[node["id"]] = node
        for identity in list(nodes):
            for link in self.graph.outgoing(scope, identity):
                if link["relation"] not in {
                    "about",
                    "has_evidence",
                    "sense_of",
                    "state_for",
                    "targets",
                    "contradicts",
                    "supersedes",
                }:
                    continue
                target = link["target_ref"]["id"]
                if target not in nodes and len(nodes) < limit:
                    node = self.graph.get(scope, target)
                    if node:
                        nodes[target] = node
                if target in nodes:
                    links.append(
                        {"source": identity, "target": target, "kind": link["relation"]}
                    )
        # SocialState's typed references already persist ownership and subject.
        # This view connects those references without writing a new edge type.
        for state in list(nodes.values()):
            if state["type"] != "SocialState":
                continue
            for pointer in (state["agent_ref"], state["subject_ref"]):
                if pointer["id"] not in nodes and len(nodes) < limit:
                    neighbour = self.graph.get(scope, pointer["id"])
                    if neighbour:
                        nodes[pointer["id"]] = neighbour
            if all(
                pointer["id"] in nodes
                for pointer in (state["agent_ref"], state["subject_ref"])
            ):
                links.append(
                    {
                        "source": state["agent_ref"]["id"],
                        "target": state["subject_ref"]["id"],
                        "kind": "relationship",
                        "weight": state["affection"],
                    }
                )
        projected = []
        for node in nodes.values():
            kind = {"Person": "person", "AgentIdentity": "bot", "Concept": "topic"}.get(
                node["type"], "memory"
            )
            record = self.record(node)
            label = record["label"]
            projected.append(
                {
                    "id": node["id"],
                    "kind": kind,
                    "label": label[:160],
                    "ref_id": node["id"],
                    "scope": scope,
                    "ontology": True,
                    "entity_type": node["type"],
                    "status": record["status"],
                    "weight": 1,
                }
            )
        return {"nodes": projected, "links": links, "schema_version": 1}

    def detail(self, scope, identity):
        node = self.graph.get(scope, identity)
        if not node:
            return None
        links = self.graph.outgoing(scope, identity)
        incoming = self.graph.incoming(scope, identity, limit=100)
        related = []
        seen = {identity}
        for link, field in [
            *((link, "target_ref") for link in links[:100]),
            *((link, "source_ref") for link in incoming),
        ]:
            target = link[field]["id"]
            if target in seen:
                continue
            seen.add(target)
            neighbour = self.graph.get(scope, target)
            if neighbour:
                related.append({**self.record(neighbour), "relation": link["relation"]})
        evidence_ids = (
            [identity]
            if node["type"] == "Evidence"
            else (
                [
                    l["target_ref"]["id"]
                    for l in links
                    if l["relation"] == "has_evidence"
                ]
                if node["type"] == "Claim"
                else [r["id"] for r in node.get("evidence_refs", [])]
            )
        )
        return {
            "node": node,
            "display": self.record(node),
            "revision": self.graph.revision(scope, identity),
            "links": links[:100],
            "incoming": incoming,
            "related": related,
            "revisions": self.graph.revision_history(scope, identity),
            "evidence": self.knowledge.evidence(scope, evidence_ids),
            "origin_evidence": self.knowledge.evidence(
                scope, [r["id"] for r in node.get("origin_evidence_refs", [])]
            ),
            "interpretation": "说法与候选不等同客观事实；首次观察不是发明来源。",
        }
