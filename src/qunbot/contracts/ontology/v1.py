"""Static V1 graph contracts and referential validation, independent of storage.

This is not a fact extractor, authorization system or graph database. It checks
structure and provenance links; semantic support still needs knowledge policy.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib

from qunbot.contracts.schema import (
    ContractError,
    HASH,
    ID,
    TIME,
    array,
    document,
    enum,
    fingerprint,
    obj,
    optional,
    text,
    validate,
)

REF = obj({"id": ID, "type": ID, "scope": ID})
REFS = array(REF, 100)
SOURCE = obj({"kind": enum("platform", "owner", "model", "legacy"), "ref": ID})
_COMMON = {
    "id": ID,
    "scope": ID,
    "schema_version": {"const": 1},
    "created_at": TIME,
    "updated_at": TIME,
    "source_refs": array(SOURCE, 100),
    "visibility": enum("group", "personal", "owner"),
}
TYPED_VALUE = {
    "oneOf": [
        obj({"kind": {"const": "text"}, "value": text(2000)}),
        obj({"kind": {"const": "number"}, "value": {"type": "number"}}),
        obj({"kind": {"const": "boolean"}, "value": {"type": "boolean"}}),
        obj({"kind": {"const": "time"}, "value": TIME}),
    ]
}
CLAIM_STATUS = enum(
    "candidate", "accepted_as_report", "disputed", "superseded", "expired", "withdrawn"
)
EXECUTION_STATUS = enum(
    "planned", "denied", "executing", "succeeded", "failed", "unknown", "cancelled"
)
PROPOSAL_STATUS = enum(
    "proposed",
    "awaiting_consent",
    "approved",
    "executing",
    "succeeded",
    "failed",
    "unknown",
    "rejected",
    "expired",
    "cancelled",
)

_PARTS = [obj({"kind": {"const": "text"}, "text": text(12000)})]
for _kind in ("image", "audio", "video", "file"):
    _PARTS.append(obj({"kind": {"const": _kind}, "asset_ref": REF}))
_PARTS += [
    obj({"kind": {"const": "face"}, "face_id": ID}),
    obj(
        {
            "kind": {"const": "at"},
            "target_ref": optional(REF),
            "all_members": {"type": "boolean"},
        }
    ),
    obj({"kind": {"const": "quote"}, "message_ref": REF}),
    obj(
        {"kind": {"const": "native"}, "action": enum("poke", "reaction", "dice", "rps")}
    ),
    obj({"kind": {"const": "unknown"}, "source_ref": SOURCE}),
]

_FIELDS = {
    "AgentIdentity": {"deployment_id": ID, "persona_version": ID},
    "Person": {"platform": enum("qq", "qq_channel"), "account_ref": ID},
    "Group": {
        "platform": enum("qq", "qq_channel"),
        "platform_group_id": ID,
        "allowed": {"type": "boolean"},
    },
    "Membership": {
        "person_ref": REF,
        "group_ref": REF,
        "nickname": text(100),
        "card": text(100),
        "role": enum("member", "admin", "owner", "unknown"),
        "status": enum("active", "left", "unknown"),
        "observed_at": TIME,
    },
    "Message": {
        "platform_message_id": optional(ID),
        "author_ref": REF,
        "event_time": TIME,
        "recorded_at": TIME,
        "direction": enum("inbound", "outbound"),
        "origin": enum("member", "agent", "operator", "system"),
        "parts": array({"oneOf": _PARTS}, 100, minimum=1),
        "reply_to_ref": optional(REF),
        "delivery_status": enum("received", "planned", "sent", "failed", "unknown"),
    },
    "MediaAsset": {
        "storage_ref": {"type": "string", "pattern": r"^asset:[A-Za-z0-9_.:-]+$"},
        "kind": enum("image", "audio", "video", "file"),
        "mime": text(100, minimum=1),
        "bytes": TIME,
        "duration_ms": optional(TIME),
        "sha256": HASH,
    },
    "ConversationEpisode": {
        "start_time": TIME,
        "end_time": TIME,
        "participants": REFS,
        "message_refs": REFS,
        "thread_refs": REFS,
        "identification": enum("explicit", "heuristic", "unknown"),
    },
    "Concept": {
        "name": text(200, minimum=1),
        "aliases": array(text(200), 50),
        "kind": enum("topic", "term", "predicate", "interest", "activity", "object"),
        "definition": text(2000),
        "status": enum("candidate", "accepted", "retired"),
        "subject_types": array(ID, 19),
        "object_types": array(ID, 19),
        "value_kinds": array(enum("text", "number", "boolean", "time"), 4),
    },
    "Claim": {
        "subject_ref": REF,
        "predicate_ref": REF,
        "object_ref": optional(REF),
        "typed_value": optional(TYPED_VALUE),
        "asserted_by_ref": REF,
        "polarity": enum("positive", "negative", "uncertain"),
        "modality": enum("assertion", "plan", "joke", "hypothesis"),
        "epistemic_status": CLAIM_STATUS,
        "source_kind": enum("self_report", "hearsay", "inferred", "platform", "legacy"),
        "observed_at": TIME,
        "recorded_at": TIME,
        "valid_from": optional(TIME),
        "valid_to": optional(TIME),
    },
    "Evidence": {
        "source_ref": REF,
        "start": TIME,
        "end": TIME,
        "source_sha256": HASH,
        "extractor_version": ID,
        "role": enum("supports", "refutes", "context"),
    },
    "SlangSense": {
        "term_ref": REF,
        "definition": text(2000, minimum=1),
        "evidence_refs": REFS,
        "usage_refs": REFS,
        "first_observed_at": TIME,
        "origin_evidence_refs": REFS,
    },
    "Interaction": {
        "kind": enum(
            "message", "reaction", "poke", "repeat_plus_one", "dice", "rps", "welcome"
        ),
        "actor_ref": REF,
        "target_ref": optional(REF),
        "message_ref": optional(REF),
        "occurred_at": TIME,
        "source_kind": enum("observed", "executed"),
        "execution_ref": optional(REF),
    },
    "Commitment": {
        "actor_ref": REF,
        "target_ref": optional(REF),
        "content": text(2000, minimum=1),
        "deadline": optional(TIME),
        "status": enum("open", "fulfilled", "cancelled", "expired", "disputed"),
        "fulfillment_refs": REFS,
    },
    "SocialState": {
        "agent_ref": REF,
        "subject_ref": optional(REF),
        "relationship_stage": text(100),
        "affection": {"type": "number", "minimum": -100, "maximum": 100},
        "emotion": obj(
            {
                name: {"type": "number", "minimum": 0, "maximum": 100}
                for name in ("valence", "energy", "stress", "interest", "sociability")
            }
        ),
        "revision": TIME,
        "decayed_at": TIME,
    },
    "Schedule": {
        "trigger_kind": enum("at", "every", "cron"),
        "trigger_value": text(200, minimum=1),
        "timezone": text(100, minimum=1),
        "capability_id": ID,
        "input_ref": ID,
        "daily_limit": TIME,
        "misfire": enum("skip", "coalesce"),
        "enabled": {"type": "boolean"},
    },
    "Proposal": {
        "kind": enum("title", "join_review"),
        "actor_ref": REF,
        "target_ref": REF,
        "content": text(2000, minimum=1),
        "status": PROPOSAL_STATUS,
        "expires_at": TIME,
        "approval_refs": REFS,
    },
    "Vote": {
        "proposal_ref": REF,
        "voter_ref": REF,
        "decision": enum("approve", "reject"),
        "message_ref": REF,
    },
    "ActionExecution": {
        "capability_id": ID,
        "capability_version": ID,
        "trigger_ref": optional(REF),
        "input_sha256": HASH,
        "authorization_ref": ID,
        "status": EXECUTION_STATUS,
        "idempotency_key": ID,
        "receipt_ref": optional(ID),
        "failure_code": optional(ID),
        "verification_refs": REFS,
    },
    "ExternalResource": {
        "service_id": ID,
        "resource_id": ID,
        "summary": text(4000),
        "result_sha256": HASH,
    },
}
_SCHEMAS = {
    kind: document(
        obj({**_COMMON, "type": {"const": kind}, **fields}), "entity:" + kind
    )
    for kind, fields in _FIELDS.items()
}

_RELATIONS = {
    "member_of": (("Membership",), ("Group",)),
    "represents": (("Membership",), ("Person",)),
    "authored": (("Person", "AgentIdentity"), ("Message",)),
    "in_scope": (tuple(t for t in _FIELDS if t != "Group"), ("Group",)),
    "replies_to": (("Message",), ("Message",)),
    "contains_media": (("Message",), ("MediaAsset",)),
    "part_of": (("Message",), ("ConversationEpisode",)),
    "about": (
        ("Claim", "ConversationEpisode", "Commitment"),
        ("Person", "Concept", "Group"),
    ),
    "has_evidence": (("Claim",), ("Evidence",)),
    "sourced_from": (("Evidence",), ("Message", "ExternalResource")),
    "contradicts": (("Claim",), ("Claim",)),
    "supersedes": (("Claim",), ("Claim",)),
    "sense_of": (("SlangSense",), ("Concept",)),
    "involved": (("Person", "AgentIdentity"), ("Interaction", "ConversationEpisode")),
    "targets": (
        ("Proposal", "Interaction", "Commitment"),
        ("Person", "Group", "Concept"),
    ),
    "vote_on": (("Vote",), ("Proposal",)),
    "triggered_by": (("ActionExecution",), ("Message", "Schedule", "Proposal")),
    "produced": (("ActionExecution",), ("Message", "MediaAsset", "ExternalResource")),
    "fulfills": (("Interaction", "ActionExecution"), ("Commitment",)),
    "state_for": (("SocialState",), ("Person", "Group", "AgentIdentity")),
    "refers_to": (("Concept", "Claim"), ("ExternalResource",)),
}
_EDGE = document(
    obj(
        {
            "id": ID,
            "scope": ID,
            "schema_version": {"const": 1},
            "relation": enum(*_RELATIONS),
            "source_ref": REF,
            "target_ref": REF,
            "source_refs": array(SOURCE, 100),
            "created_at": TIME,
        }
    ),
    "edge",
)

# Semantic targets for reference fields; references are checked against real nodes.
_REF_TYPES = {
    "Membership": {"person_ref": ("Person",), "group_ref": ("Group",)},
    "Message": {
        "author_ref": ("Person", "AgentIdentity"),
        "reply_to_ref": ("Message",),
    },
    "ConversationEpisode": {
        "participants": ("Person", "AgentIdentity"),
        "message_refs": ("Message",),
        "thread_refs": ("ConversationEpisode",),
    },
    "Claim": {
        "subject_ref": ("Person", "Group", "AgentIdentity", "Concept"),
        "predicate_ref": ("Concept",),
        "object_ref": (
            "Person",
            "Group",
            "AgentIdentity",
            "Concept",
            "ExternalResource",
        ),
        "asserted_by_ref": ("Person", "AgentIdentity"),
    },
    "Evidence": {"source_ref": ("Message", "ExternalResource")},
    "SlangSense": {
        "term_ref": ("Concept",),
        "evidence_refs": ("Evidence",),
        "usage_refs": ("Message",),
        "origin_evidence_refs": ("Evidence",),
    },
    "Interaction": {
        "actor_ref": ("Person", "AgentIdentity"),
        "target_ref": ("Person", "Group", "Concept"),
        "message_ref": ("Message",),
        "execution_ref": ("ActionExecution",),
    },
    "Commitment": {
        "actor_ref": ("Person", "AgentIdentity"),
        "target_ref": ("Person", "Group", "Concept"),
        "fulfillment_refs": ("Interaction", "ActionExecution"),
    },
    "SocialState": {
        "agent_ref": ("AgentIdentity",),
        "subject_ref": ("Person", "Group"),
    },
    "Proposal": {
        "actor_ref": ("Person", "AgentIdentity"),
        "target_ref": ("Person",),
        "approval_refs": ("Vote", "Message"),
    },
    "Vote": {
        "proposal_ref": ("Proposal",),
        "voter_ref": ("Person",),
        "message_ref": ("Message",),
    },
    "ActionExecution": {
        "trigger_ref": ("Message", "Schedule", "Proposal"),
        "verification_refs": ("Message", "ExternalResource"),
    },
}


def entity_types() -> tuple[str, ...]:
    return tuple(sorted(_SCHEMAS))


def relation_types() -> tuple[str, ...]:
    return tuple(sorted(_RELATIONS))


def entity_schema(kind: str) -> dict:
    if not isinstance(kind, str) or kind not in _SCHEMAS:
        raise ContractError("Unknown ontology entity type")
    return deepcopy(_SCHEMAS[kind])


def specification() -> dict:
    return deepcopy(
        {
            "version": 1,
            "entities": _SCHEMAS,
            "edge": _EDGE,
            "relations": {
                k: {"source_types": list(v[0]), "target_types": list(v[1])}
                for k, v in _RELATIONS.items()
            },
            "reference_targets": _REF_TYPES,
        }
    )


def specification_hash() -> str:
    return fingerprint(specification())


def _ref_key(ref: dict) -> tuple[str, str]:
    return ref["scope"], ref["id"]


def validate_graph(nodes: list[dict], edges: list[dict], *, scope: str) -> None:
    """Validate a complete scoped projection, not arbitrary partially loaded nodes."""
    validate(ID, scope)
    if (
        not isinstance(nodes, list)
        or not isinstance(edges, list)
        or len(nodes) > 10000
        or len(edges) > 20000
    ):
        raise ContractError("Graph projection exceeds contract bounds")
    index = {}
    identities = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ContractError("Node must be an object")
        validate(entity_schema(node.get("type", "")), node)
        if node["scope"] != scope:
            raise ContractError("Node outside trusted scope")
        if node["updated_at"] < node["created_at"]:
            raise ContractError("Invalid node time order")
        key = _ref_key(node)
        if key in index:
            raise ContractError("Duplicate node identity")
        index[key] = node
        if node["type"] == "Person":
            identity = (node["platform"], node["account_ref"])
            if identity in identities:
                raise ContractError("Duplicate person platform identity")
            identities.add(identity)

    def resolve(ref: dict, allowed: tuple[str, ...]) -> dict:
        if ref["scope"] != scope:
            raise ContractError("Reference outside trusted scope")
        target = index.get(_ref_key(ref))
        if (
            target is None
            or target["type"] != ref["type"]
            or target["type"] not in allowed
        ):
            raise ContractError("Missing or incompatible reference target")
        return target

    for node in nodes:
        kind = node["type"]
        for field, allowed in _REF_TYPES.get(kind, {}).items():
            value = node[field]
            for ref in (
                value
                if isinstance(value, list)
                else ([value] if value is not None else [])
            ):
                resolve(ref, allowed)
        if kind == "Message":
            for part in node["parts"]:
                for field, allowed in (
                    ("asset_ref", ("MediaAsset",)),
                    ("message_ref", ("Message",)),
                    ("target_ref", ("Person", "Group")),
                ):
                    if part.get(field) is not None:
                        target = resolve(part[field], allowed)
                        if field == "asset_ref" and target["kind"] != part["kind"]:
                            raise ContractError(
                                "Message part disagrees with asset kind"
                            )
                if part["kind"] == "at" and part["all_members"] == (
                    part["target_ref"] is not None
                ):
                    raise ContractError("At must select a target or all members")
            if node["direction"] == "inbound" and node["delivery_status"] != "received":
                raise ContractError("Inbound message cannot claim delivery")
            if (
                node["direction"] == "outbound"
                and node["delivery_status"] == "received"
            ):
                raise ContractError("Outbound message cannot claim inbound receipt")
        if kind == "Concept" and any(
            t not in _SCHEMAS for t in (*node["subject_types"], *node["object_types"])
        ):
            raise ContractError("Concept refers to unknown entity types")
        if kind == "Claim":
            if (node["object_ref"] is None) == (node["typed_value"] is None):
                raise ContractError("Claim requires exactly one object or typed value")
            predicate = resolve(node["predicate_ref"], ("Concept",))
            if predicate["kind"] != "predicate":
                raise ContractError("Claim predicate must be a predicate concept")
            if node["subject_ref"]["type"] not in predicate["subject_types"]:
                raise ContractError("Predicate does not allow subject type")
            if (
                node["object_ref"] is not None
                and node["object_ref"]["type"] not in predicate["object_types"]
            ):
                raise ContractError("Predicate does not allow object type")
            if (
                node["typed_value"] is not None
                and node["typed_value"]["kind"] not in predicate["value_kinds"]
            ):
                raise ContractError("Predicate does not allow literal value kind")
            if (
                node["source_kind"] == "self_report"
                and node["subject_ref"] != node["asserted_by_ref"]
            ):
                raise ContractError("Self-report subject must be the reporting actor")
            if (
                node["epistemic_status"] != "candidate"
                and predicate["status"] != "accepted"
            ):
                raise ContractError("Accepted report requires an accepted predicate")
            if (
                node["valid_from"] is not None
                and node["valid_to"] is not None
                and node["valid_to"] < node["valid_from"]
            ):
                raise ContractError("Invalid claim validity window")
        if kind == "Evidence":
            if node["end"] <= node["start"]:
                raise ContractError("Evidence requires a nonempty source span")
            source = resolve(node["source_ref"], ("Message", "ExternalResource"))
            source_text = (
                "".join(p["text"] for p in source["parts"] if p["kind"] == "text")
                if source["type"] == "Message"
                else source["summary"]
            )
            if node["end"] > len(source_text):
                raise ContractError("Evidence span exceeds readable source text")
            if (
                node["source_sha256"]
                != hashlib.sha256(source_text.encode()).hexdigest()
            ):
                raise ContractError("Evidence source fingerprint mismatch")
        if kind == "ConversationEpisode" and node["end_time"] < node["start_time"]:
            raise ContractError("Invalid episode time order")

    seen_edges, seen_links, support, votes = set(), set(), set(), set()
    for edge in edges:
        validate(_EDGE, edge)
        if edge["scope"] != scope:
            raise ContractError("Edge outside trusted scope")
        if edge["id"] in seen_edges:
            raise ContractError("Duplicate edge identity")
        seen_edges.add(edge["id"])
        source_types, target_types = _RELATIONS[edge["relation"]]
        source, target = (
            resolve(edge["source_ref"], source_types),
            resolve(edge["target_ref"], target_types),
        )
        if source["id"] == target["id"]:
            raise ContractError("Self-relation is not allowed in V1")
        link = (edge["relation"], source["id"], target["id"])
        if link in seen_links:
            raise ContractError("Duplicate relation")
        seen_links.add(link)
        if edge["relation"] == "has_evidence" and target["role"] == "supports":
            support.add(source["id"])
        # Structural edges must agree with equivalent authoritative fields.
        mirror = {
            "member_of": "group_ref",
            "represents": "person_ref",
            "replies_to": "reply_to_ref",
            "sourced_from": "source_ref",
            "sense_of": "term_ref",
            "vote_on": "proposal_ref",
            "triggered_by": "trigger_ref",
        }.get(edge["relation"])
        if mirror and source[mirror] != edge["target_ref"]:
            raise ContractError("Relation disagrees with object reference")
        if (
            edge["relation"] == "authored"
            and target["author_ref"] != edge["source_ref"]
        ):
            raise ContractError("Authorship relation disagrees with message")
    for node in nodes:
        if (
            node["type"] == "Claim"
            and node["epistemic_status"] == "accepted_as_report"
            and node["id"] not in support
        ):
            raise ContractError(
                "Accepted report requires supporting evidence, not context"
            )
        if node["type"] == "Vote":
            key = (node["proposal_ref"]["id"], node["voter_ref"]["id"])
            if key in votes:
                raise ContractError("Duplicate voter for proposal")
            votes.add(key)
            message = resolve(node["message_ref"], ("Message",))
            if message["author_ref"] != node["voter_ref"]:
                raise ContractError("Vote source must be authored by the voter")
