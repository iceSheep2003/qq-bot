"""Read-only evidence and referential audits. Not semantic gold evaluation."""

from __future__ import annotations

from collections import Counter
import hashlib

from qunbot.contracts.schema import ContractError, validate
from qunbot.contracts.ontology.objects import references
from qunbot.contracts.ontology.v1 import validate_graph, specification


class GraphQuality:
    def __init__(self, graph):
        self.graph = graph

    def _closure(self, scope, node):
        nodes = {}
        edges = {}
        pending = [node]
        while pending:
            current = pending.pop()
            if current["id"] in nodes:
                continue
            nodes[current["id"]] = current
            for reference in references(current):
                other = self.graph.get(scope, reference["id"])
                if not other:
                    raise ContractError("Missing audited reference")
                pending.append(other)
            for link in self.graph.outgoing(scope, current["id"], "has_evidence"):
                edges[link["id"]] = link
                other = self.graph.get(scope, link["target_ref"]["id"])
                if not other:
                    raise ContractError("Missing audited evidence")
                pending.append(other)
        validate_graph(list(nodes.values()), list(edges.values()), scope=scope)

    def audit(self, scopes):
        stats = Counter()
        failures = []
        spec = specification()
        for scope in scopes:
            index = {}
            for kind in spec["entities"]:
                after = ""
                while True:
                    batch = self.graph.list_nodes(scope, kind, limit=1000, after=after)
                    if not batch:
                        break
                    for node in batch:
                        index[node["id"]] = node
                    after = batch[-1]["id"]
            # Offline audits may read a whole bounded group once. Writes still
            # use affected closures. Re-expanding quote chains per episode
            # made the old audit quadratic on real hot-chat histories.
            if len(index) > 200000:
                raise ContractError("Offline audit partition exceeds budget")

            def resolve(reference):
                target = index.get(reference["id"])
                if (
                    reference["scope"] != scope
                    or not target
                    or target["type"] != reference["type"]
                ):
                    raise ContractError("Invalid audit reference")
                return target

            supported = set()
            after = ""
            while True:
                batch = self.graph.edge_page(scope, after=after)
                if not batch:
                    break
                for link in batch:
                    stats["edges_checked"] += 1
                    try:
                        validate(spec["edge"], link)
                        source, target = (
                            resolve(link["source_ref"]),
                            resolve(link["target_ref"]),
                        )
                        allowed = spec["relations"][link["relation"]]
                        if (
                            source["type"] not in allowed["source_types"]
                            or target["type"] not in allowed["target_types"]
                            or source["id"] == target["id"]
                        ):
                            raise ContractError("Invalid edge endpoints")
                        if (
                            link["relation"] == "has_evidence"
                            and target["role"] == "supports"
                        ):
                            supported.add(source["id"])
                    except ContractError:
                        failures.append(
                            {"scope": scope, "id": link["id"], "reason": "invalid_edge"}
                        )
                after = batch[-1]["id"]
            for node in index.values():
                kind = node["type"]
                stats[kind + "_checked"] += 1
                try:
                    validate(spec["entities"][kind], node)
                    for reference in references(node):
                        resolve(reference)
                    if kind == "Evidence":
                        source = resolve(node["source_ref"])
                        text = (
                            "".join(
                                p["text"]
                                for p in source["parts"]
                                if p["kind"] == "text"
                            )
                            if source["type"] == "Message"
                            else source["summary"]
                        )
                        if (
                            not 0 <= node["start"] < node["end"] <= len(text)
                            or hashlib.sha256(
                                text[node["start"] : node["end"]].encode()
                            ).hexdigest()
                            != node["source_sha256"]
                        ):
                            raise ContractError("Invalid source evidence")
                    if kind == "Claim":
                        stats["status:" + node["epistemic_status"]] += 1
                        if (
                            node["epistemic_status"] == "accepted_as_report"
                            and node["id"] not in supported
                        ):
                            raise ContractError(
                                "Accepted report without supporting edge"
                            )
                        if (
                            node["source_kind"] == "self_report"
                            and node["subject_ref"] != node["asserted_by_ref"]
                        ):
                            raise ContractError("Invalid self report")
                except ContractError:
                    failures.append(
                        {
                            "scope": scope,
                            "id": node["id"],
                            "reason": "invalid_reference_or_evidence",
                        }
                    )
        return {
            "checks": dict(stats),
            "failures": failures,
            "failure_count": len(failures),
            "foreign_key_violations": self.graph.stats()["foreign_key_violations"],
            "semantic_accuracy": None,
            "limitations": "Evidence span validity does not prove the paraphrase or modality correct. Human semantic gold labels are not yet available.",
        }
