"""Equivalent wire-schema normalization for OpenAI-compatible providers.

DeepSeek requires an explicit top-level object type, even when every oneOf
branch already requires one. Adding that redundant type preserves validation
semantics. Host contracts and branch constraints are never removed or loosened.
"""

from __future__ import annotations
from copy import deepcopy


def model_tool_projection(tools):
    if not tools:
        return tools
    result = deepcopy(tools)
    for tool in result:
        schema = tool.get("function", {}).get("parameters", {})
        if (
            schema.get("type") is None
            and isinstance(schema.get("oneOf"), list)
            and schema["oneOf"]
        ):
            if all(branch.get("type") == "object" for branch in schema["oneOf"]):
                schema["type"] = "object"
    return result
