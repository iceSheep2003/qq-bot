from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from qunbot.extensions.loader import build_features, build_registry
from qunbot.scheduling import JobHandlerRegistry
from qunbot.storage.conversation import ConversationStore
from qunbot.storage.database import SqliteDatabase
from qunbot.storage.jobs import JobsStore
from qunbot.storage.memory import MemoryStore
from qunbot.storage.relationships import RelationshipsStore


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
            {"chat", "continuation"},
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
