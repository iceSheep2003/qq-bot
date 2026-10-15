"""Fixed model-facing tool contracts; descriptors are contracts, not handlers.

The kernel is intentionally closed. New behavior is implemented in trusted
code and exposed through an owner-enabled Skill; no external tool catalogue or
runtime protocol can add model-facing tools.
"""

from __future__ import annotations

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

_SOURCE_REFS = array(ID, 50)
_ITEM = obj(
    {"id": ID, "label": text(200), "summary": text(2000), "source_refs": _SOURCE_REFS}
)
_ERROR = obj(
    {
        "code": enum(
            "denied", "invalid_input", "unavailable", "upstream_failure", "unknown"
        ),
        "message": text(500),
    }
)
_KNOWLEDGE_HIT = obj(
    {
        "claim_id": ID,
        "subject_id": ID,
        "predicate": text(200),
        "summary": text(2000),
        "status": enum(
            "candidate",
            "accepted_as_report",
            "disputed",
            "superseded",
            "expired",
            "withdrawn",
        ),
        "source_kind": enum("self_report", "hearsay", "inferred", "platform", "legacy"),
        "evidence_ids": array(ID, 20),
        "score": {"type": "number"},
    }
)
def _result(data: dict, name: str) -> dict:
    schema = obj(
        {
            "status": enum(
                "ok", "denied", "invalid_input", "unavailable", "failed", "unknown"
            ),
            "trace_id": ID,
            "data": optional(data),
            "error": optional(_ERROR),
        }
    )
    schema["allOf"] = [
        {
            "if": {"properties": {"status": {"const": "ok"}}},
            "then": {"properties": {"data": data, "error": {"type": "null"}}},
            "else": {"properties": {"data": {"type": "null"}, "error": _ERROR}},
        }
    ]
    return document(schema, "tool-output:" + name)


_ACTION = obj(
    {
        "request_id": ID,
        "kind": ID,
        "state": enum(
            "planned",
            "denied",
            "awaiting_consent",
            "approved",
            "executing",
            "succeeded",
            "failed",
            "unknown",
        ),
    }
)
_GROUP_ACTION_INPUT = {
    "oneOf": [
        obj(
            {
                "kind": {"const": "mute"},
                "target_id": ID,
                "duration_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 2592000,
                },
                "reason": text(300),
            }
        ),
        obj({"kind": {"const": "unmute"}, "target_id": ID}),
        obj(
            {
                "kind": {"const": "essence"},
                "message_id": ID,
                "enable": {"type": "boolean"},
            }
        ),
        obj(
            {
                "kind": {"const": "title"},
                "target_id": ID,
                "title": text(30, minimum=1),
                "reason": text(300),
            }
        ),
        obj(
            {
                "kind": {"const": "join_review"},
                "request_id": ID,
                "approve": {"type": "boolean"},
                "reason": text(300),
            }
        ),
    ]
}
_INTERACTION_INPUT = {
    "oneOf": [
        obj({"kind": {"const": "reaction"}, "message_id": ID, "emoji_id": ID}),
        obj({"kind": {"const": "poke"}, "target_id": ID}),
        obj({"kind": {"const": "repeat_plus_one"}, "message_id": ID}),
        obj({"kind": enum("dice", "rps")}),
    ]
}
_DEFINITIONS = {
    "conversation_read": (
        obj(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "message_ids": array(ID, 20),
            },
            ("limit",),
        ),
        obj(
            {
                "messages": array(
                    obj(
                        {
                            "id": ID,
                            "author_id": ID,
                            "text": text(12000),
                            "event_time": TIME,
                            "direction": enum("inbound", "outbound"),
                            "delivery_status": enum(
                                "received", "sent", "failed", "unknown"
                            ),
                            "asset_ids": array(ID, 20),
                            "reply_to_id": optional(ID),
                        }
                    ),
                    100,
                )
            }
        ),
        "read",
        "Read bounded actual dialogue in the current scope.",
    ),
    "knowledge_search": (
        obj(
            {
                "query": text(1200, minimum=1),
                "subject_id": ID,
                "category": enum(
                    "person", "relation", "slang", "event", "topic", "commitment"
                ),
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "as_of": TIME,
            },
            ("query", "limit"),
        ),
        obj({"hits": array(_KNOWLEDGE_HIT, 20)}),
        "read",
        "Find scoped knowledge reports with evidence and uncertainty.",
    ),
    "knowledge_evidence": (
        obj({"evidence_ids": array(ID, 20, minimum=1)}),
        obj(
            {
                "evidence": array(
                    obj(
                        {
                            "id": ID,
                            "source_id": ID,
                            "excerpt": text(2000),
                            "role": enum("supports", "refutes", "context"),
                            "source_sha256": HASH,
                        }
                    ),
                    20,
                )
            }
        ),
        "read",
        "Read allowed evidence snippets, not unrelated conversation history.",
    ),
    "group_query": (
        obj(
            {
                "category": enum("members", "files", "proposals"),
                "query": text(200),
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ("category", "limit"),
        ),
        obj(
            {
                "category": enum("members", "files", "proposals"),
                "items": array(_ITEM, 50),
            }
        ),
        "read",
        "Query current-group members, file descriptions or proposal state.",
    ),
    "media_catalog": (
        obj(
            {
                "category": enum("meme", "qq_face", "native", "voice"),
                "tag": text(100),
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ("category", "limit"),
        ),
        obj({"available": {"type": "boolean"}, "items": array(_ITEM, 50)}),
        "read",
        "List safe media references and available delivery capabilities.",
    ),
    "vision_analyze": (
        obj({"asset_id": ID, "question": text(1200, minimum=1)}),
        obj(
            {
                "asset_id": ID,
                "analysis": text(6000),
                "uncertainty": enum("low", "medium", "high"),
                "source_refs": _SOURCE_REFS,
            }
        ),
        "perception",
        "Analyze an allowed image asset, never an arbitrary URL.",
    ),
    "audio_transcribe": (
        obj({"asset_id": ID}),
        obj(
            {
                "asset_id": ID,
                "transcript": text(6000),
                "confidence": optional({"type": "number", "minimum": 0, "maximum": 1}),
                "source_refs": _SOURCE_REFS,
            }
        ),
        "perception",
        "Transcribe an allowed audio asset; unknown confidence remains null.",
    ),
    "interaction_propose": (
        _INTERACTION_INPUT,
        _ACTION,
        "command",
        "Propose a native interaction; host policy decides execution.",
    ),
    "group_action_propose": (
        _GROUP_ACTION_INPUT,
        _ACTION,
        "command",
        "Propose a configured group operation; never self-authorize.",
    ),
    "skill_load": (
        obj({"skill_id": ID}),
        obj(
            {
                "skill_id": ID,
                "version": ID,
                "instructions": text(12000),
                "required_capabilities": array(ID, 50),
            }
        ),
        "read",
        "Load owner-enabled skill instructions; not install or execute code.",
    ),
}


def tool_names() -> tuple[str, ...]:
    return tuple(sorted(_DEFINITIONS))


def _definition(name: str) -> tuple:
    if not isinstance(name, str) or name not in _DEFINITIONS:
        raise ContractError("Unknown kernel tool")
    return _DEFINITIONS[name]


def tool_input_schema(name: str) -> dict:
    return document(_definition(name)[0], "tool-input:" + name)


def tool_output_schema(name: str) -> dict:
    return _result(_definition(name)[1], name)


def model_tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _definition(name)[3],
                "parameters": tool_input_schema(name),
            },
        }
        for name in tool_names()
    ]


def specification() -> dict:
    return {
        "version": 1,
        "tools": {
            name: {
                "input": tool_input_schema(name),
                "output": tool_output_schema(name),
                "mode": _definition(name)[2],
                "description": _definition(name)[3],
            }
            for name in tool_names()
        },
    }


def specification_hash() -> str:
    return fingerprint(specification())


def validate_tool_input(name: str, arguments: dict) -> None:
    validate(tool_input_schema(name), arguments, max_bytes=16384)


def validate_tool_output(name: str, result: dict) -> None:
    validate(tool_output_schema(name), result, max_bytes=65536)


