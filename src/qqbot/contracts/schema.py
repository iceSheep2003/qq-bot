"""JSON Schema helpers shared by static contracts, not a plugin registration API."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from functools import lru_cache

from jsonschema import Draft202012Validator, FormatChecker

DIALECT = "https://json-schema.org/draft/2020-12/schema"
ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": 160,
    "pattern": r"^(?!.*://)(?!.*//)[A-Za-z0-9][A-Za-z0-9_.:@/-]*$",
}
TIME = {"type": "integer", "minimum": 0}
HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$"}


class ContractError(ValueError):
    """Safe diagnostic: never includes rejected values or private chat text."""


def text(maximum: int = 2000, *, minimum: int = 0) -> dict:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def array(item: dict, maximum: int = 100, *, minimum: int = 0) -> dict:
    return {
        "type": "array",
        "items": deepcopy(item),
        "minItems": minimum,
        "maxItems": maximum,
    }


def optional(schema: dict) -> dict:
    return {"anyOf": [deepcopy(schema), {"type": "null"}]}


def obj(properties: dict[str, dict], required: tuple[str, ...] | None = None) -> dict:
    return {
        "type": "object",
        "properties": deepcopy(properties),
        "required": list(properties if required is None else required),
        "additionalProperties": False,
    }


def document(schema: dict, name: str) -> dict:
    return {"$schema": DIALECT, "$id": "urn:qqbot:v1:" + name, **deepcopy(schema)}


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ContractError("Payload must be finite, serializable JSON") from None


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def validate(schema: dict, value: Any, *, max_bytes: int = 65536) -> None:
    if len(canonical(value)) > max_bytes:
        raise ContractError("Payload exceeds contract byte budget")
    # Schemas are supplied by versioned code, not by a message or tool result.
    validator = _compiled_validator(canonical(schema))
    error = next(validator.iter_errors(value), None)
    if error is not None:
        # Paths may contain untrusted object keys; report only the schema keyword.
        raise ContractError(f"Contract validation failed ({error.validator})")


@lru_cache(maxsize=256)
def _compiled_validator(schema_bytes: bytes):
    """Cache schemas only. Never cache private instances or rejected values."""
    return Draft202012Validator(
        json.loads(schema_bytes), format_checker=FormatChecker()
    )
