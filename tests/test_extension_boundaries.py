from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from qunbot.domain import MessageEvent
from qunbot.extensions.loader import build_features, build_registry
from qunbot.runtime.tools import (
    DEFAULT_TOOL_QUOTA_PER_MINUTE,
    Tool,
    ToolPermission,
    ToolRegistry,
    built_in_tools,
)
from qunbot.scheduling import JobHandlerRegistry
from qunbot.storage.conversation import ConversationStore
from qunbot.storage.database import SqliteDatabase
from qunbot.storage.jobs import JobsStore
from qunbot.storage.memory import MemoryStore
from qunbot.storage.relationships import RelationshipsStore


def message(text: str = "你好", *, user_id: str = "7", scope: str = "group:42"):
    return MessageEvent(
        f"e{user_id}", scope, scope.split(":")[-1], user_id, "小明", text, (), True, (), 0
    )


class ExtensionBoundaryTests(unittest.TestCase):
    def config(self, *names: str):
        return SimpleNamespace(
            extensions=frozenset(names)
        )

    def test_only_enabled_actions_are_registered(self):
        # scheduled_chat owns both water-the-group strategies: the fixed-time
        # "chat" action and the post-message interval "continuation" action.
        self.assertEqual(
            build_registry(self.config("scheduled_chat")).actions(),
            {"chat", "deliver", "continuation"},
        )
        self.assertEqual(build_registry(self.config()).actions(), set())

    def test_poster_does_not_load_for_chat_only(self):
        sys.modules.pop("qunbot.extensions.exam_poster.renderer", None)
        build_registry(self.config("scheduled_chat"))
        self.assertNotIn("qunbot.extensions.exam_poster.renderer", sys.modules)

    def test_unknown_extension_fails_at_startup(self):
        with self.assertRaisesRegex(ValueError, "unknown extensions"):
            build_registry(self.config("unreviewed_package"))

    def test_registry_rejects_duplicate_actions(self):
        from qunbot.extensions.scheduled_chat.job import ChatJobHandler

        registry = JobHandlerRegistry()
        registry.register(ChatJobHandler())
        with self.assertRaisesRegex(ValueError, "duplicate job action"):
            registry.register(ChatJobHandler())

    def test_core_does_not_import_optional_features(self):
        root = Path(__file__).resolve().parents[1] / "qunbot"
        for relative in ("runtime/agent.py", "runtime/service.py", "config.py", "ports.py", "memory/service.py", "scheduling/scheduler.py"):
            with self.subTest(relative=relative):
                tree = ast.parse((root / relative).read_text(encoding="utf-8"))
                imports = [
                    node.module or ""
                    for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom)
                ]
                self.assertFalse(any("extensions" in name for name in imports))

    def test_disabled_features_contribute_nothing(self):
        host = build_features(self.config(), object())
        self.assertEqual(host.context.collect(object()), {})
        self.assertEqual(host.observers, [])
        self.assertIsNone(host.media())

    def test_repositories_have_independent_instances(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "bot.sqlite3")
            try:
                repositories = [
                    ConversationStore(database), RelationshipsStore(database),
                    MemoryStore(database), JobsStore(database),
                ]
                self.assertEqual(len({type(repo) for repo in repositories}), 4)
                self.assertTrue(all(repo.db is database.db for repo in repositories))
            finally:
                database.close()


class _Clock:
    """Monotonic test clock, so quota windows are exercised without sleeping."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def echo_tool(name: str = "echo", **overrides) -> Tool:
    fields = {
        "name": name,
        "description": "echo",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "handler": lambda args, _event: f"echo:{args['text']}",
    }
    fields.update(overrides)
    return Tool(**fields)


class ToolPermissionTests(unittest.TestCase):
    """The model cannot reach authority the deployer never granted."""

    def test_privileged_permissions_are_refused_at_registration(self):
        for permission in (
            ToolPermission.MODIFY_AFFECTION,
            ToolPermission.MODIFY_PERSONA,
            ToolPermission.CREATE_JOB,
            ToolPermission.NETWORK,
            ToolPermission.FILESYSTEM,
        ):
            with self.subTest(permission=permission.name):
                registry = ToolRegistry()
                with self.assertRaisesRegex(ValueError, "privileged permission"):
                    registry.register(
                        echo_tool("danger", permissions=permission)
                    )
                self.assertEqual(registry.schemas(), [])

    def test_a_privileged_tool_cannot_be_smuggled_in_with_a_grantable_one(self):
        registry = ToolRegistry()
        with self.assertRaisesRegex(ValueError, "modify_affection"):
            registry.register(
                echo_tool(
                    "mixed",
                    permissions=(
                        ToolPermission.READ_MEMORY | ToolPermission.MODIFY_AFFECTION
                    ),
                )
            )

    def test_grantable_permissions_are_accepted(self):
        registry = ToolRegistry()
        registry.register(
            echo_tool(
                "reader",
                permissions=ToolPermission.READ_MEMORY | ToolPermission.READ_CONVERSATION,
            )
        )
        self.assertEqual(
            registry.permissions("reader"),
            ["read_conversation", "read_memory"],
        )

    def test_duplicate_tool_names_are_refused(self):
        registry = ToolRegistry()
        registry.register(echo_tool("echo"))
        with self.assertRaisesRegex(ValueError, "duplicate tool"):
            registry.register(echo_tool("echo"))

    def test_the_built_in_tool_is_read_only_and_attributed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            database = SqliteDatabase(Path(directory) / "bot.sqlite3")
            try:
                registry = built_in_tools(MemoryStore(database))
                self.assertEqual(registry.names(), ["recall_memory"])
                self.assertEqual(
                    registry.permissions("recall_memory"), ["read_memory"]
                )
                self.assertEqual(registry.get("recall_memory").source, "core")
                # The built-in takes the registry's default allowance rather
                # than a larger one; nothing here is special-cased.
                self.assertEqual(
                    registry.quota_per_minute("recall_memory"),
                    DEFAULT_TOOL_QUOTA_PER_MINUTE,
                )
            finally:
                database.close()


class ToolQuotaTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.registry = ToolRegistry(clock=self.clock, window_seconds=60.0)
        self.registry.register(echo_tool("echo", quota_per_minute=2))

    def test_within_quota_the_tool_runs(self):
        self.assertEqual(
            self.registry.call("echo", {"text": "a"}, message()), "echo:a"
        )

    def test_exceeding_the_quota_is_a_refusal_the_model_can_read(self):
        self.registry.call("echo", {"text": "a"}, message())
        self.registry.call("echo", {"text": "b"}, message())
        with self.assertLogs("qunbot.runtime.tools", level="WARNING"):
            third = self.registry.call("echo", {"text": "c"}, message())
        self.assertIn("quota exceeded", third)
        self.assertIn("2/min", third)
        self.assertNotIn("echo:c", third)

    def test_the_window_slides(self):
        self.registry.call("echo", {"text": "a"}, message())
        self.registry.call("echo", {"text": "b"}, message())
        self.clock.advance(61.0)
        self.assertEqual(
            self.registry.call("echo", {"text": "c"}, message()), "echo:c"
        )

    def test_quota_is_per_conversation(self):
        self.registry.call("echo", {"text": "a"}, message(scope="group:42"))
        self.registry.call("echo", {"text": "b"}, message(scope="group:42"))
        self.assertEqual(
            self.registry.call("echo", {"text": "c"}, message(scope="group:43")),
            "echo:c",
        )

    def test_a_quota_refusal_is_a_string_not_an_exception(self):
        self.registry.call("echo", {"text": "a"}, message())
        self.registry.call("echo", {"text": "b"}, message())
        with self.assertLogs("qunbot.runtime.tools", level="WARNING"):
            refused = self.registry.call("echo", {"text": "c"}, message())
        self.assertIsInstance(refused, str)


class ToolArgumentTests(unittest.TestCase):
    def setUp(self):
        self.ran: list[dict] = []

        def handler(args, _event):
            self.ran.append(args)
            return "ran"

        self.registry = ToolRegistry()
        self.registry.register(echo_tool("echo", handler=handler))

    def test_a_missing_required_argument_is_refused_before_the_handler(self):
        with self.assertRaisesRegex(ValueError, "missing required"):
            self.registry.call("echo", {}, message())
        self.assertEqual(self.ran, [])

    def test_a_wrong_type_is_refused_before_the_handler(self):
        with self.assertRaisesRegex(ValueError, "should be string"):
            self.registry.call("echo", {"text": 12}, message())
        self.assertEqual(self.ran, [])

    def test_unknown_keys_are_ignored_not_fatal(self):
        self.assertEqual(
            self.registry.call("echo", {"text": "a", "extra": 1}, message()), "ran"
        )

    def test_oversized_arguments_are_refused(self):
        with self.assertRaisesRegex(ValueError, "exceed"):
            self.registry.call("echo", {"text": "x" * 5000}, message())
        self.assertEqual(self.ran, [])

    def test_non_object_arguments_are_refused(self):
        with self.assertRaisesRegex(ValueError, "object of arguments"):
            self.registry.call("echo", ["text"], message())
        self.assertEqual(self.ran, [])

    def test_an_unknown_tool_is_reported_and_audited(self):
        self.assertEqual(self.registry.call("nope", {}, message()), "unknown tool")
        entry = self.registry.audit_log()[-1]
        self.assertEqual((entry.tool, entry.allowed), ("nope", False))
        self.assertEqual(entry.reason, "unknown tool")


class ToolAuditTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.registry = ToolRegistry(clock=self.clock, audit_size=4)
        self.registry.register(echo_tool("echo", quota_per_minute=1))

    def test_a_successful_call_is_recorded_with_its_source_and_speaker(self):
        self.registry.call("echo", {"text": "a"}, message(user_id="9"))
        entry = self.registry.audit_log()[-1]
        self.assertEqual(entry.tool, "echo")
        self.assertEqual(entry.scope, "group:42")
        self.assertEqual(entry.user_id, "9")
        self.assertEqual(entry.source, "core")
        self.assertTrue(entry.allowed)
        self.assertEqual(entry.reason, "ok")

    def test_a_refused_call_is_recorded_too(self):
        self.registry.call("echo", {"text": "a"}, message())
        with self.assertLogs("qunbot.runtime.tools", level="WARNING"):
            self.registry.call("echo", {"text": "b"}, message())
        entry = self.registry.audit_log()[-1]
        self.assertFalse(entry.allowed)
        self.assertEqual(entry.reason, "quota exceeded")

    def test_an_invalid_call_is_recorded_and_reraised(self):
        with self.assertRaises(ValueError):
            self.registry.call("echo", {}, message())
        entry = self.registry.audit_log()[-1]
        self.assertFalse(entry.allowed)
        self.assertIn("missing required", entry.reason)

    def test_the_audit_log_is_bounded(self):
        for index in range(10):
            self.registry.call("nope", {"i": index}, message())
        self.assertEqual(len(self.registry.audit_log()), 4)

    def test_audit_entries_are_json_shaped(self):
        self.registry.call("echo", {"text": "a"}, message())
        encoded = self.registry.audit_log()[-1].as_dict()
        self.assertEqual(
            sorted(encoded),
            ["allowed", "reason", "scope", "source", "tool", "ts", "user_id"],
        )
