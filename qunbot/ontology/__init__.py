"""Fixed ontology V1. Public schemas are defensive copies, with no register API."""

from .v1 import (
    entity_schema,
    entity_types,
    relation_types,
    specification,
    specification_hash,
    validate_graph,
)

__all__ = [
    "entity_schema",
    "entity_types",
    "relation_types",
    "specification",
    "specification_hash",
    "validate_graph",
]
