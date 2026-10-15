"""Queries and corrections over the fixed ontology. Claims remain reports."""

from __future__ import annotations

import hashlib
import time

from qunbot.contracts.schema import ContractError
from qunbot.contracts.ontology.objects import edge, entity, ref, stable_id


class KnowledgeService:
    def __init__(self, graph):
        self.graph = graph

    def add_report(
        self,
        scope: str,
        *,
        message_id: str,
        subject_id: str,
        predicate: dict,
        start: int,
        end: int,
        value: str,
        source_kind="inferred",
        modality="assertion",
        polarity="positive",
        extractor_version="extractor-v1",
        accepted=False,
    ) -> str:
        source = self.graph.get(scope, message_id)
        subject = self.graph.get(scope, subject_id)
        if not source or source["type"] != "Message" or not subject:
            raise ContractError("Knowledge source or subject is unavailable")
        if source["delivery_status"] not in {"received", "sent"}:
            raise ContractError("Unconfirmed delivery cannot support knowledge")
        text = "".join(p["text"] for p in source["parts"] if p["kind"] == "text")
        if not 0 <= start < end <= len(text) or not value.strip() or len(value) > 2000:
            raise ContractError("Knowledge requires a bounded exact evidence span")
        if accepted and (
            source_kind != "self_report"
            or subject_id != source["author_ref"]["id"]
            or modality != "assertion"
        ):
            raise ContractError(
                "Only explicit self-reports may be accepted automatically"
            )
        stamp = source["event_time"]
        sources = [
            {"kind": "model", "ref": extractor_version},
            *source["source_refs"][:20],
        ]
        evidence = entity(
            "Evidence",
            scope,
            stable_id("evidence", scope, message_id, start, end, extractor_version),
            stamp,
            sources=sources,
            source_ref=ref(source),
            start=start,
            end=end,
            source_sha256=hashlib.sha256(text[start:end].encode()).hexdigest(),
            extractor_version=extractor_version,
            role="supports",
        )
        identity = stable_id(
            "claim", scope, message_id, subject_id, predicate["id"], start, end, value
        )
        existing = self.graph.get(scope, identity)
        if existing:
            return identity  # Never revive withdrawn/superseded claims on replay.
        claim = entity(
            "Claim",
            scope,
            identity,
            stamp,
            sources=sources,
            subject_ref=ref(subject),
            predicate_ref=ref(predicate),
            object_ref=None,
            typed_value={"kind": "text", "value": value},
            asserted_by_ref=source["author_ref"],
            polarity=polarity,
            modality=modality,
            epistemic_status="accepted_as_report" if accepted else "candidate",
            source_kind=source_kind,
            observed_at=stamp,
            recorded_at=int(time.time() * 1000),
            valid_from=None,
            valid_to=None,
        )
        self.graph.write(
            scope,
            [predicate, evidence, claim],
            [
                edge("has_evidence", claim, evidence, stamp),
                edge("sourced_from", evidence, source, stamp),
                edge("about", claim, subject, stamp),
            ],
            reason="evidence-extraction",
        )
        return identity

    def search(
        self,
        scope: str,
        query: str,
        limit=5,
        *,
        subject_id=None,
        as_of=None,
        include_candidates=False,
        category=None,
    ) -> list[dict]:
        hits = []
        now = int(time.time() * 1000) if as_of is None else as_of
        for claim, score in self.graph.search_claims(
            scope, query, limit=200, subject_id=subject_id
        ):
            status = claim["epistemic_status"]
            if status != "accepted_as_report" and not (
                include_candidates and status == "candidate"
            ):
                continue
            if (
                claim["observed_at"] > now
                or (claim["valid_from"] is not None and claim["valid_from"] > now)
                or (claim["valid_to"] is not None and claim["valid_to"] <= now)
            ):
                continue
            predicate = self.graph.get(scope, claim["predicate_ref"]["id"])
            if not predicate:
                continue
            if category and category not in predicate["aliases"]:
                continue
            evidences = [
                e["target_ref"]["id"]
                for e in self.graph.outgoing(scope, claim["id"], "has_evidence")
            ]
            validated_evidence = self.evidence(scope, evidences)
            if not validated_evidence:
                continue
            value = claim["typed_value"]
            hits.append(
                {
                    "claim_id": claim["id"],
                    "subject_id": claim["subject_ref"]["id"],
                    "predicate": predicate["name"],
                    "summary": str(value["value"])
                    if value
                    else "entity relationship report",
                    "status": status,
                    "source_kind": claim["source_kind"],
                    "evidence_ids": [e["id"] for e in validated_evidence],
                    "score": score,
                }
            )
            if len(hits) >= min(20, max(1, limit)):
                break
        return hits

    def evidence(self, scope: str, identities: list[str]) -> list[dict]:
        result = []
        for identity in identities[:20]:
            evidence = self.graph.get(scope, identity)
            if (
                not evidence
                or evidence["type"] != "Evidence"
                or evidence["visibility"] != "group"
            ):
                continue
            source = self.graph.get(scope, evidence["source_ref"]["id"])
            if not source:
                continue
            if source["type"] == "Message" and source["delivery_status"] not in {"received", "sent"}:
                continue
            text = (
                "".join(p["text"] for p in source["parts"] if p["kind"] == "text")
                if source["type"] == "Message"
                else source["summary"]
            )
            excerpt = text[evidence["start"] : evidence["end"]]
            if (
                hashlib.sha256(excerpt.encode()).hexdigest()
                != evidence["source_sha256"]
            ):
                continue
            result.append(
                {
                    "id": identity,
                    "source_id": source["id"],
                    "excerpt": excerpt[:2000],
                    "role": evidence["role"],
                    "source_sha256": evidence["source_sha256"],
                }
            )
        return result

    def revise(
        self,
        scope: str,
        identity: str,
        status: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> None:
        if status not in {"disputed", "superseded", "expired", "withdrawn"}:
            raise ContractError("Invalid knowledge correction")
        claim = self.graph.get(scope, identity)
        if not claim or claim["type"] != "Claim":
            raise ContractError("Claim is unavailable")
        claim["epistemic_status"] = status
        claim["updated_at"] = max(claim["updated_at"], int(time.time() * 1000))
        self.graph.write(
            scope,
            [claim],
            [],
            reason=reason,
            expected_revisions={identity: expected_revision},
        )
