import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qunbot.contracts.kernel_v1 import tool_names
from qunbot.contracts.schema import ContractError
from qunbot.actions.tools.kernel import KernelTools
from qunbot.infrastructure.storage.database import SqliteDatabase
from qunbot.infrastructure.storage.executions import ExecutionStore
from qunbot.infrastructure.adapters.schema_projection import model_tool_projection


class KernelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(Path(self.tmp.name) / "test.db")
        self.event = SimpleNamespace(
            scope="group:42", event_id="event:1", origin="human"
        )
        self.executions = ExecutionStore(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    async def test_wire_projection_preserves_fixed_contract_and_union_constraints(self):
        from qunbot.contracts.kernel_v1 import model_tool_schemas, validate_tool_input

        original = model_tool_schemas()
        projected = model_tool_projection(original)
        group_original = next(
            t for t in original if t["function"]["name"] == "group_action_propose"
        )
        group_wire = next(
            t for t in projected if t["function"]["name"] == "group_action_propose"
        )
        self.assertNotIn("type", group_original["function"]["parameters"])
        self.assertEqual(group_wire["function"]["parameters"]["type"], "object")
        self.assertEqual(
            group_wire["function"]["parameters"]["oneOf"],
            group_original["function"]["parameters"]["oneOf"],
        )
        with self.assertRaises(ContractError):
            validate_tool_input(
                "group_action_propose", {"kind": "title", "duration_seconds": 60}
            )

    async def test_tool_surface_is_fixed_and_scope_cannot_be_overridden(self):
        tools = KernelTools(
            {"conversation_read": lambda a, e: {"messages": []}},
            allowed_scopes=frozenset({"group:42"}),
        )
        self.assertEqual(
            [t["function"]["name"] for t in tools.schemas()], list(tool_names())
        )
        result = json.loads(
            await tools.acall(
                "conversation_read", {"limit": 5, "scope": "group:other"}, self.event
            )
        )
        self.assertEqual(result["status"], "invalid_input")
        self.event.scope = "group:other"
        result = json.loads(
            await tools.acall("conversation_read", {"limit": 5}, self.event)
        )
        self.assertEqual(result["status"], "denied")

    async def test_uncertain_side_effect_is_never_automatically_repeated(self):
        calls = []

        async def handler(a, e):
            calls.append(a)
            raise TimeoutError()

        tools = KernelTools(
            {"interaction_propose": handler},
            allowed_scopes=frozenset({"group:42"}),
            executions=self.executions,
        )
        one = json.loads(
            await tools.acall("interaction_propose", {"kind": "dice"}, self.event)
        )
        two = json.loads(
            await tools.acall("interaction_propose", {"kind": "dice"}, self.event)
        )
        self.assertEqual(one, two)
        self.assertEqual(one["status"], "unknown")
        self.assertEqual(len(calls), 1)

    async def test_restart_marks_pending_execution_unknown(self):
        self.executions.begin("group:42", "execution:1", "interaction_propose", "a" * 64)
        self.assertEqual(self.executions.recover_uncertain(), 1)
        row = self.executions.begin("group:42", "execution:1", "interaction_propose", "a" * 64)
        self.assertEqual(row["state"], "unknown")
