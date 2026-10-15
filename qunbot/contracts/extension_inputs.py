"""The only public extension input is an owner-enabled Skill declaration.

Validation does not load a file, launch a process, connect to a service, or
grant permission. Those responsibilities remain with future host wiring.
"""

from copy import deepcopy
from .schema import (
    ID,
    array,
    document,
    enum,
    fingerprint,
    obj,
    text,
    validate,
)

_SKILL = document(
    obj(
        {
            "name": ID,
            "version": ID,
            "description": text(1000, minimum=1),
            "activation": enum("always", "on_demand"),
            "required_capabilities": array(ID, 50),
            "instructions_path": {
                "type": "string",
                "maxLength": 240,
                "pattern": r"^(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9][A-Za-z0-9_./-]*\.md$",
            },
            "context_budget_chars": {"type": "integer", "minimum": 1, "maximum": 12000},
            "priority": {"type": "integer", "minimum": -100, "maximum": 100},
        }
    ),
    "skill-declaration",
)

# Optional addition: old owner declarations remain valid and group-only.
_SKILL["properties"]["audiences"] = array(enum("group", "private"), 2, minimum=1)
_SKILL["properties"]["audiences"]["uniqueItems"] = True

def specification() -> dict:
    return deepcopy({"version": 1, "skill": _SKILL})


def specification_hash() -> str:
    return fingerprint(specification())


def validate_skill(value: dict) -> None:
    validate(_SKILL, value, max_bytes=16384)


