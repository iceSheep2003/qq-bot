"""Deterministic host-owned identities and document constructors, not model APIs."""

from __future__ import annotations

import hashlib


def stable_id(kind: str, *keys: object) -> str:
    import json

    encoded = json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode()
    return kind + ":" + hashlib.sha256(encoded).hexdigest()[:32]


def ref(node: dict) -> dict:
    return {key: node[key] for key in ("id", "type", "scope")}


def entity(
    entity_type: str, scope: str, identity: str, timestamp: int, *, sources=(), **fields
) -> dict:
    return {
        "id": identity,
        "type": entity_type,
        "scope": scope,
        "schema_version": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "source_refs": list(sources),
        "visibility": "group",
        **fields,
    }


def edge(
    relation: str, source: dict, target: dict, timestamp: int, *, sources=()
) -> dict:
    return {
        "id": stable_id("edge", source["scope"], relation, source["id"], target["id"]),
        "scope": source["scope"],
        "schema_version": 1,
        "relation": relation,
        "source_ref": ref(source),
        "target_ref": ref(target),
        "created_at": timestamp,
        "source_refs": list(sources),
    }


def references(value):
    """Walk typed entity references; source_refs are provenance, not foreign keys."""
    if isinstance(value, dict):
        if set(value) == {"id", "type", "scope"}:
            yield value
        else:
            for item in value.values():
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)
