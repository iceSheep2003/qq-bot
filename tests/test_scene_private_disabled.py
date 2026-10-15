"""Additional negative acceptance: disabled private tools stay disabled."""

import asyncio
import inspect
import json

from qunbot.harness.tools import KernelTools
from test_scene_refactor import event


def test_disabling_private_tools_prevents_even_a_valid_peer_from_calling():
    assert "private_enabled" in inspect.signature(KernelTools).parameters
    calls = []
    tools = KernelTools({"conversation_read": lambda a, e: calls.append(a)},
                        allowed_scopes=frozenset(), private_enabled=False)
    result = json.loads(asyncio.run(tools.acall("conversation_read", {"limit": 5}, event())))
    assert result["status"] == "denied"
    assert calls == []
