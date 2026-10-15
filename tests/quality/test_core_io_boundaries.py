"""Core decisions depend on ports, never open SQL or HTTP providers themselves."""
import ast
import importlib
import importlib.util
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from qqbot.agent.memory.service import MemoryService
from qqbot.contracts.models import BotPolicy, ConversationPolicy, SchedulePolicy
from tests.support import Store

ROOT = Path(__file__).resolve().parents[2]


def module(test, name):
    test.assertIsNotNone(importlib.util.find_spec(name), f'Missing canonical adapter: {name}')
    return importlib.import_module(name)


class CoreIoBoundaries(unittest.TestCase):
    def test_only_contracts_define_bot_policy(self):
        definitions = []
        for path in (ROOT / 'src/qqbot').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ClassDef) and node.name == 'BotPolicy':
                    definitions.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(definitions, ['src/qqbot/contracts/models.py'])
        service = importlib.import_module('qqbot.agent.service')
        self.assertFalse(hasattr(service, 'BotPolicy'))

    def test_core_imports_no_sql_or_http_client(self):
        forbidden = {'sqlite3', 'httpx', 'aiohttp', 'requests'}
        imports = []
        for path in (ROOT / 'src/qqbot/agent').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                names = [node.module or ''] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
                imports.extend((path.name, name) for name in names if name.split('.')[0] in forbidden)
        self.assertEqual(imports, [])

    def test_default_memory_service_does_not_construct_an_embedding_client(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / 'bot.sqlite3')
            try:
                with mock.patch.dict(os.environ, {'BOT_MEMORY_EMBEDDING_PROVIDER':'openai', 'BOT_MEMORY_EMBEDDING_MODEL':'test', 'BOT_MEMORY_EMBEDDING_BASE_URL':'http://unused.test'}), mock.patch('httpx.Client') as client:
                    service = MemoryService(SimpleNamespace(), store, store)
                self.assertFalse(hasattr(service, "embeddings"))
                client.assert_not_called()
            finally:
                store.db.close()

    def test_emotion_system_requires_an_injected_repository(self):
        from qqbot.agent.identity.emotion.system import EmotionSystem
        self.assertIn('store', inspect.signature(EmotionSystem).parameters)
        self.assertFalse((ROOT / 'src/qqbot/agent/identity/emotion/store.py').exists())
        ports = importlib.import_module('qqbot.contracts.ports')
        self.assertTrue(hasattr(ports, 'EmotionRepository'))

    def test_emotion_storage_has_one_canonical_adapter(self):
        adapter = module(self, 'qqbot.infrastructure.storage.emotion')
        with tempfile.TemporaryDirectory() as temp:
            store = adapter.EmotionStore(Path(temp) / 'mood.sqlite3')
            try:
                self.assertEqual(store.load('private:7'), {})
                from qqbot.agent.identity.emotion.state import DIMENSIONS
                values = dict.fromkeys(DIMENSIONS, 50.0)
                self.assertTrue(store.change('group:42', values, {'valence':1}, 'test', dedupe_key='e1', now=10))
                self.assertFalse(store.change('group:42', values, {'valence':1}, 'test', dedupe_key='e1', now=10))
                self.assertEqual(store.event_count('group:42'), 1)
                self.assertEqual(store.load('private:7'), {})
            finally:
                store.close()
                store.close()

    def test_emotion_can_run_with_an_in_memory_port_without_opening_files(self):
        from qqbot.agent.identity.emotion.system import EmotionSystem
        from qqbot.agent.identity.emotion.config import EmotionConfig
        self.assertIn('store', inspect.signature(EmotionSystem).parameters)
        store = SimpleNamespace(load=lambda scope: {}, events=lambda scope: (), close=lambda: None)
        config = EmotionConfig(True, False, Path('/unused/mood.sqlite3'), 180, 1, 35)
        system = EmotionSystem(config, SimpleNamespace(), store=store, clock=lambda: 100)
        self.assertTrue(system.narration('private:7'))
        self.assertEqual(system.history('private:7'), ())
        system.close()

    def test_mood_factory_is_composed_outside_core(self):
        composition = module(self, 'qqbot.features.mood.composition')
        self.assertTrue(callable(composition.build_emotion))
        core = importlib.import_module('qqbot.agent.identity.emotion')
        self.assertFalse(hasattr(core, 'build_emotion'))

    def test_embedding_feature_has_no_active_module_or_construction_hook(self):
        from qqbot.infrastructure.storage.memory import MemoryStore
        self.assertFalse((ROOT / 'src/qqbot/agent/memory/embeddings.py').exists())
        self.assertFalse((ROOT / 'src/qqbot/infrastructure/adapters/embeddings.py').exists())
        self.assertNotIn('embeddings', inspect.signature(MemoryService).parameters)
        self.assertNotIn('embedder', inspect.signature(MemoryStore).parameters)

    def test_canonical_policy_preserves_scheduling_and_private_flags(self):
        policy = BotPolicy(ConversationPolicy(frozenset({'42'}), 8, 'UTC', False, True), SchedulePolicy(daily_limit=3))
        self.assertTrue(policy.private_enabled)
        self.assertEqual(policy.job_daily_limit, 3)

    def test_no_old_storage_imports_survive(self):
        old = 'qqbot.agent.identity.emotion.store'
        found = []
        for path in (ROOT / 'src/qqbot').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module == old:
                    found.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(found, [])

    def test_public_service_policy_annotation_resolves_to_the_canonical_type(self):
        from typing import get_type_hints
        from qqbot.agent.service import ConversationService
        resolved, error = {}, None
        try:
            resolved = get_type_hints(ConversationService.__init__)
        except (NameError, TypeError) as exc:
            error = str(exc)
        self.assertIsNone(error, 'Public constructor annotations must be inspectable')
        self.assertIs(resolved['policy'], BotPolicy)
