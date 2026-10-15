"""Retired embedding functionality cannot return through configuration or old DBs."""
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from qqbot.agent.memory import ranking
from qqbot.agent.memory.service import MemoryService
from qqbot.infrastructure.storage.database import SqliteDatabase
from qqbot.infrastructure.storage.memory import MemoryStore

ROOT = Path(__file__).resolve().parents[2]


class EmbeddingRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'bot.sqlite3'
        self.database = SqliteDatabase(self.path)
        self.store = MemoryStore(self.database)

    def tearDown(self):
        self.database.close()
        self.tmp.cleanup()

    def test_memory_has_no_provider_parameter_or_vector_methods(self):
        self.assertNotIn('embeddings', inspect.signature(MemoryService).parameters)
        self.assertNotIn('embedder', inspect.signature(MemoryStore).parameters)
        self.assertFalse(hasattr(self.store, 'attach_embedder'))
        self.assertFalse(hasattr(self.store, 'reindex_embeddings'))

    def test_embedding_modules_are_deleted(self):
        self.assertFalse((ROOT / 'src/qqbot/agent/memory/embeddings.py').exists())
        self.assertFalse((ROOT / 'src/qqbot/infrastructure/adapters/embeddings.py').exists())

    def test_new_database_has_no_vector_columns(self):
        columns = {r[1] for r in self.database.db.execute('PRAGMA table_info(memories)')}
        self.assertFalse(columns & {'embedding', 'embedding_model'})

    def test_history_vectors_are_preserved_but_never_exposed_or_used(self):
        columns = {r[1] for r in self.database.db.execute('PRAGMA table_info(memories)')}
        for name, kind in [('embedding','BLOB'),('embedding_model','TEXT')]:
            if name not in columns:
                self.database.db.execute(f'ALTER TABLE memories ADD COLUMN {name} {kind}')
        identity = self.store.observe('group:42','7','小明喜欢蓝莓蛋糕')['memory_id']
        self.database.db.execute('UPDATE memories SET embedding=?,embedding_model=? WHERE id=?',
                                 (b'historical-vector','retired-provider',identity))
        self.database.close()
        self.database = SqliteDatabase(self.path)
        self.store = MemoryStore(self.database)
        rows = self.store.candidates('group:42','蓝莓蛋糕',subject_user_id='7')
        self.assertEqual(len(rows),1)
        self.assertFalse({'vector','embedding','embedding_model'} & rows[0].keys())
        self.assertEqual(self.database.db.execute('SELECT embedding FROM memories WHERE id=?',(identity,)).fetchone()[0], b'historical-vector')
        json.dumps(rows)

    def test_vector_fields_have_no_effect_on_ranking(self):
        base={'id':1,'scope':'group:42','user_id':'7','content':'已确认事实','created_at':100,'lexical':0.1}
        ordinary = ranking.rank([base],now=100)[0]
        injected = ranking.rank([{**base,'vector':1.0}],now=100)[0]
        self.assertEqual(ordinary.score,injected.score)
        self.assertFalse(any('vector=' in reason for reason in injected.reasons))
        self.assertFalse(hasattr(ranking.DEFAULT_WEIGHTS,'vector'))

    def test_statistics_describe_memory_without_vector_metrics(self):
        self.store.observe('group:42','7','小明喜欢蓝莓蛋糕')
        self.assertEqual(self.store.stats('group:42'), {'by_status':{'active':1},'total':1})

    def test_active_configuration_does_not_offer_embedding(self):
        self.assertNotIn('BOT_MEMORY_EMBEDDING_', (ROOT / '.env.example').read_text())
        self.assertNotIn('BOT_MEMORY_EMBEDDING_', (ROOT / 'docs/guides/FEATURES.md').read_text())
        source=(ROOT / 'src/qqbot/bootstrap/app.py').read_text()
        self.assertNotIn('embeddings_from_env',source)

    def test_memory_service_works_without_embedding_state(self):
        service=MemoryService(SimpleNamespace(),self.store,self.store)
        self.assertFalse(hasattr(service,'embeddings'))
        self.assertEqual(service.related_context('group:42','蓝莓蛋糕')['items'], [])
