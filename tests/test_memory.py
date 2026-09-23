"""Long-term memory: schema migration, write path, retrieval, lifecycle.

Everything here is a unit test against a temporary SQLite file. Nothing
connects to NapCat, a model endpoint or a real embedding service.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from support import Store
from qunbot.domain import MessageEvent
from qunbot.memory import dedupe, ranking, text
from qunbot.memory.embeddings import HashingEmbedder, embeddings_from_env
from qunbot.memory.models import MemoryItem, RetrievalHit
from qunbot.memory.service import MemoryService
from qunbot.runtime.agent import Agent
from qunbot.runtime.skills import SkillCatalog
from qunbot.runtime.tools import built_in_tools
from qunbot.storage.database import SqliteDatabase
from qunbot.storage.memory import MemoryStore, dedupe_key, normalize_content
from qunbot.storage.privacy import forget_user

SCOPE = "group:42"
OTHER_SCOPE = "group:43"

LEGACY_SCHEMA = """
CREATE TABLE memories (
  id INTEGER PRIMARY KEY, scope TEXT NOT NULL, user_id TEXT NOT NULL,
  content TEXT NOT NULL, importance INTEGER NOT NULL DEFAULT 1,
  source_event_id TEXT, created_at INTEGER NOT NULL
);
CREATE VIRTUAL TABLE memories_fts
  USING fts5(content, content='memories', content_rowid='id');
CREATE TRIGGER memory_insert AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid,content) VALUES(new.id,new.content);
END;
CREATE TRIGGER memory_delete AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts,rowid,content)
  VALUES('delete',old.id,old.content);
END;
"""


class FailingEmbedder:
    name = "always-fails"

    def embed(self, texts):
        raise RuntimeError("provider down")


class FakeModel:
    def __init__(self, payload: str = "[]"):
        self.payload = payload
        self.calls = 0

    async def complete(self, messages, tools=None, *, temperature=0.7):
        self.calls += 1
        return {"choices": [{"message": {"content": self.payload}}], "usage": {}}


class FakeSkills:
    def catalog_text(self):
        return ""

    def select(self, text, *, proactive=False):
        return []

    def schemas(self):
        return []

    def call(self, name, args, event):
        return ""


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "bot.sqlite3"
        self.database = SqliteDatabase(self.path)
        self.store = MemoryStore(self.database)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    # ------------------------------------------------------------- migration

    def test_legacy_database_migrates_without_losing_rows(self):
        legacy = self.root / "legacy.sqlite3"
        raw = sqlite3.connect(legacy)
        raw.executescript(LEGACY_SCHEMA)
        raw.execute(
            "INSERT INTO memories(scope,user_id,content,importance,source_event_id,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (SCOPE, "7", "小明喜欢蓝莓蛋糕", 4, "m1", 1000),
        )
        raw.execute(
            "INSERT INTO memories(scope,user_id,content,importance,source_event_id,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (SCOPE, "7", "小明喜欢蓝莓蛋糕。", 1, "m2", 1001),
        )
        raw.commit()
        raw.close()

        database = SqliteDatabase(legacy)
        try:
            columns = {
                row[1] for row in database.db.execute("PRAGMA table_info(memories)")
            }
            for column in (
                "fact_type",
                "confidence",
                "status",
                "visibility",
                "expires_at",
                "access_count",
                "dedupe_key",
                "embedding",
                "updated_at",
            ):
                self.assertIn(column, columns)
            store = MemoryStore(database)
            rows = store.list_memories(SCOPE)
            self.assertEqual(len(rows), 2, "both legacy rows must survive")
            statuses = sorted(row["status"] for row in rows)
            self.assertEqual(statuses, ["active", "superseded"])
            survivor = [row for row in rows if row["status"] == "active"][0]
            self.assertEqual(survivor["content"], "小明喜欢蓝莓蛋糕")
            self.assertTrue(survivor["dedupe_key"])
            self.assertEqual(survivor["updated_at"], survivor["created_at"])
            fts_sql = database.db.execute(
                "SELECT sql FROM sqlite_master WHERE name='memories_fts'"
            ).fetchone()[0]
            self.assertIn("trigram", fts_sql)
            self.assertEqual(len(store.search_memories(SCOPE, "蓝莓蛋糕")), 1)
        finally:
            database.close()

    def test_migration_is_idempotent(self):
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", source_event_id="m1")
        self.database.close()
        reopened = SqliteDatabase(self.path)
        try:
            store = MemoryStore(reopened)
            self.assertEqual(len(store.list_memories(SCOPE)), 1)
            tables = reopened.db.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='memories_fts'"
            ).fetchone()[0]
            self.assertEqual(tables, 1)
            self.assertEqual(len(store.search_memories(SCOPE, "蓝莓蛋糕")), 1)
        finally:
            reopened.close()
            self.database = SqliteDatabase(self.path)  # for tearDown

    # -------------------------------------------------------------- retrieval

    def test_chinese_substring_search_uses_trigram_index(self):
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", source_event_id="m1")
        rows = self.store.candidates(SCOPE, "喜欢蓝莓")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match"], "fts")
        rows = self.store.candidates(SCOPE, "蓝莓蛋")
        self.assertEqual(len(rows), 1)

    def test_full_sentence_query_still_matches(self):
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", source_event_id="m1")
        self.store.observe(SCOPE, "7", "小红在准备考研", source_event_id="m2")
        for query in ("小明，你今天想吃蓝莓蛋糕吗？", "你想吃蓝莓蛋糕吗", "蓝莓蛋糕在哪"):
            with self.subTest(query=query):
                rows = self.store.candidates(SCOPE, query)
                self.assertEqual(
                    [row["content"] for row in rows][:1], ["小明喜欢蓝莓蛋糕"]
                )
                self.assertEqual(rows[0]["match"], "fts")

    def test_short_query_falls_back_to_like(self):
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        rows = self.store.candidates(SCOPE, "蓝莓")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match"], "like")

    def test_search_results_are_json_serialisable(self):
        self.store.attach_embedder(HashingEmbedder(64))
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        rows = self.store.search_memories(SCOPE, "蓝莓蛋糕")
        json.dumps(rows)  # the recall tool dumps rows for the model

    def test_scopes_do_not_bleed(self):
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        self.assertEqual(self.store.candidates(OTHER_SCOPE, "蓝莓蛋糕"), [])
        self.assertEqual(self.store.search_memories(OTHER_SCOPE, "蓝莓蛋糕"), [])

    def test_personal_memories_need_a_matching_subject(self):
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", visibility="personal")
        self.store.observe(SCOPE, "8", "小红喜欢草莓", visibility="personal")
        self.store.observe(SCOPE, "_group_", "群里每周五开黑", visibility="group")
        for_user_7 = {row["content"] for row in self.store.candidates(
            SCOPE, "喜欢蓝莓蛋糕 草莓 每周五", subject_user_id="7"
        )}
        self.assertIn("小明喜欢蓝莓蛋糕", for_user_7)
        self.assertNotIn("小红喜欢草莓", for_user_7)
        self.assertIn("群里每周五开黑", for_user_7)
        anonymous = {row["content"] for row in self.store.candidates(SCOPE, "喜欢草莓")}
        self.assertNotIn("小红喜欢草莓", anonymous)

    # ------------------------------------------------------------- write path

    def test_observe_is_idempotent(self):
        first = self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", confidence=0.9)
        second = self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", confidence=0.9)
        self.assertEqual(first["outcome"], "inserted")
        self.assertEqual(second["outcome"], "reinforced")
        self.assertEqual(second["memory_id"], first["memory_id"])
        self.assertGreater(second["confidence"], first["confidence"])
        self.assertEqual(len(self.store.list_memories(SCOPE)), 1)

    def test_punctuation_and_case_variants_collapse(self):
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕。")
        self.store.observe(SCOPE, "7", " 小明 喜欢蓝莓蛋糕 ")
        self.assertEqual(len(self.store.list_memories(SCOPE)), 1)
        self.assertTrue(self.store.has_memory(SCOPE, "7", "小明喜欢蓝莓蛋糕。"))

    def test_has_memory_matches_containment(self):
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        self.assertTrue(self.store.has_memory(SCOPE, "7", "小明喜欢蓝莓蛋糕，尤其是奶油"))
        self.assertFalse(self.store.has_memory(SCOPE, "7", "小红喜欢草莓"))

    def test_low_confidence_stays_a_candidate_until_reinforced(self):
        self.store.observe(SCOPE, "7", "小明可能搬到杭州", confidence=0.4)
        self.assertEqual(self.store.list_memories(SCOPE)[0]["status"], "candidate")
        self.assertEqual(self.store.candidates(SCOPE, "杭州"), [])
        self.assertEqual(
            len(self.store.candidates(SCOPE, "杭州", include_candidates=True)), 1
        )
        for _ in range(3):
            self.store.observe(SCOPE, "7", "小明可能搬到杭州", confidence=0.4)
        row = self.store.list_memories(SCOPE)[0]
        self.assertEqual(row["status"], "active")
        self.assertEqual(len(self.store.candidates(SCOPE, "杭州")), 1)

    # --------------------------------------------------------------- hygiene

    def test_normalisation_helpers(self):
        self.assertEqual(normalize_content(" 小明，喜欢 蓝莓 "), "小明喜欢蓝莓")
        self.assertEqual(
            dedupe_key(SCOPE, "7", "小明，喜欢蓝莓"),
            dedupe_key(SCOPE, "7", "小明喜欢蓝莓"),
        )
        self.assertNotEqual(
            dedupe_key(SCOPE, "7", "小明喜欢蓝莓"), dedupe_key(SCOPE, "8", "小明喜欢蓝莓")
        )

    def test_similarity_and_tokenisation(self):
        self.assertGreater(text.similarity("小明喜欢蓝莓蛋糕", "小明喜欢蓝莓蛋糕。"), 0.9)
        self.assertLess(text.similarity("小明喜欢蓝莓蛋糕", "今天天气不错"), 0.2)
        self.assertIn("蓝莓", text.query_tokens("小明喜欢蓝莓蛋糕"))

    def test_dedupe_decisions(self):
        existing = [{"id": 1, "content": "小明喜欢蓝莓蛋糕"}]
        self.assertEqual(dedupe.decide(existing, "小明喜欢蓝莓蛋糕。").kind, "duplicate")
        self.assertEqual(
            dedupe.decide(existing, "小明喜欢蓝莓蛋糕和奶油").kind, "paraphrase"
        )
        self.assertEqual(dedupe.decide(existing, "小红养了一只猫").kind, "new")
        self.assertTrue(dedupe.contradiction("小明喜欢蓝莓蛋糕", "小明不喜欢蓝莓蛋糕"))
        self.assertFalse(dedupe.contradiction("小明喜欢蓝莓蛋糕", "小明喜欢蓝莓蛋糕"))

    # --------------------------------------------------------------- vectors

    def test_embedding_attach_stores_vectors_and_ranks(self):
        store = self.store.attach_embedder(HashingEmbedder(128))
        store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", source_event_id="m1")
        store.observe(SCOPE, "7", "小红在准备考研", source_event_id="m2")
        stats = store.stats(SCOPE)
        self.assertEqual(stats["embedded"], 2)
        hits = ranking.rank(
            store.candidates(SCOPE, "蓝莓蛋糕"),
            subject_user_id="7",
            limit=2,
        )
        self.assertTrue(any(row.get("vector") is not None for row in store.candidates(
            SCOPE, "蓝莓蛋糕"
        )))
        self.assertEqual(hits[0].item.content, "小明喜欢蓝莓蛋糕")
        self.assertTrue(any("vector=" in reason for reason in hits[0].reasons))

    def test_broken_embedder_degrades_to_lexical(self):
        store = self.store.attach_embedder(FailingEmbedder())
        with self.assertLogs("qunbot.storage.memory", level="WARNING"):
            outcome = store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", source_event_id="m1")
            self.assertEqual(outcome["outcome"], "inserted")
            rows = store.candidates(SCOPE, "蓝莓蛋糕")
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0].get("vector"))
            self.assertEqual(store.stats(SCOPE)["embedded"], 0)
            self.assertEqual(store.reindex_embeddings(SCOPE), 0)

    def test_embedding_provider_is_off_by_default(self):
        self.assertIsNone(embeddings_from_env({}))
        self.assertIsNone(embeddings_from_env({"BOT_MEMORY_EMBEDDING_PROVIDER": ""}))
        with self.assertLogs("qunbot.memory.embeddings", level="WARNING"):
            self.assertIsNone(
                embeddings_from_env({"BOT_MEMORY_EMBEDDING_PROVIDER": "openai"})
            )
            self.assertIsNone(
                embeddings_from_env({"BOT_MEMORY_EMBEDDING_PROVIDER": "nonsense"})
            )
        hashing = embeddings_from_env(
            {"BOT_MEMORY_EMBEDDING_PROVIDER": "hash", "BOT_MEMORY_EMBEDDING_DIM": "64"}
        )
        self.assertIsNotNone(hashing)
        self.assertEqual(len(hashing.embed(["小明"])[0]), 64)
        offline = embeddings_from_env(
            {
                "BOT_MEMORY_EMBEDDING_PROVIDER": "openai",
                "BOT_MEMORY_EMBEDDING_MODEL": "bge-m3",
                "BOT_MEMORY_EMBEDDING_BASE_URL": "http://127.0.0.1:1",
            }
        )
        self.assertIsNotNone(offline)
        with self.assertLogs("qunbot.memory.embeddings", level="WARNING"):
            self.assertIsNone(offline.embed(["小明"]))  # unreachable -> no vectors

    # ------------------------------------------------------------- lifecycle

    def test_expiry_removes_memory_from_retrieval(self):
        now = int(time.time())
        self.store.observe(SCOPE, "7", "小明这周在出差", expires_at=now - 1)
        self.assertEqual(self.store.candidates(SCOPE, "出差"), [])
        self.assertEqual(self.store.expire(now), 1)
        self.assertEqual(self.store.list_memories(SCOPE)[0]["status"], "expired")

    def test_archive_retires_stale_low_value_rows(self):
        now = int(time.time())
        outcome = self.store.observe(SCOPE, "7", "小明以前用旧手机")
        self.database.db.execute(
            "UPDATE memories SET created_at=? WHERE id=?", (now - 400 * 86400, outcome["memory_id"])
        )
        self.assertEqual(self.store.candidates(SCOPE, "旧手机")[0]["id"], outcome["memory_id"])
        self.assertEqual(self.store.archive(now, older_than_days=120), 1)
        self.assertEqual(self.store.candidates(SCOPE, "旧手机"), [])

    def test_access_reinforcement(self):
        memory_id = self.store.observe(
            SCOPE, "7", "小明喜欢蓝莓蛋糕", confidence=0.7
        )["memory_id"]
        self.store.reinforce([memory_id])
        self.store.reinforce([memory_id])
        row = self.store.get(memory_id)
        self.assertEqual(row["access_count"], 2)
        self.assertIsNotNone(row["last_accessed_at"])
        self.assertGreater(row["confidence"], 0.7)

    def test_maintain_reports_work(self):
        self.store.observe(SCOPE, "7", "小明这周在出差", expires_at=int(time.time()) - 5)
        report = self.store.maintain()
        self.assertEqual(report["expired"], 1)
        self.assertEqual(self.store.stats(SCOPE)["by_status"], {"expired": 1})

    def test_forget_removes_row_fts_entry_and_vector(self):
        store = self.store.attach_embedder(HashingEmbedder(64))
        memory_id = store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕")["memory_id"]
        self.assertEqual(len(store.candidates(SCOPE, "蓝莓蛋糕")), 1)
        self.assertEqual(store.forget([memory_id]), 1)
        self.assertIsNone(store.get(memory_id))
        self.assertEqual(store.candidates(SCOPE, "蓝莓蛋糕"), [])
        remaining = self.database.db.execute(
            "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH ?",
            ("蓝莓蛋糕",),
        ).fetchone()[0]
        self.assertEqual(remaining, 0)
        self.assertEqual(store.stats(SCOPE)["total"], 0)

    def test_forget_scope_clears_everything_in_it(self):
        store = self.store.attach_embedder(HashingEmbedder(64))
        store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        store.observe(SCOPE, "8", "小红喜欢草莓", visibility="personal")
        store.observe(OTHER_SCOPE, "7", "小明喜欢蓝莓蛋糕")
        self.assertEqual(store.forget_scope(SCOPE), 2)
        self.assertEqual(store.list_memories(SCOPE), [])
        self.assertEqual(len(store.list_memories(OTHER_SCOPE)), 1)

    def test_privacy_deletion_also_clears_indexes(self):
        store = self.store.attach_embedder(HashingEmbedder(64))
        store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕", visibility="personal")
        store.observe(SCOPE, "8", "小红喜欢草莓", visibility="personal")
        forget_user(self.database, "7")
        self.assertEqual(store.stats(SCOPE)["total"], 1)
        self.assertEqual(store.candidates(SCOPE, "蓝莓蛋糕", subject_user_id="7"), [])
        self.assertEqual(
            self.database.db.execute(
                "SELECT count(*) FROM memories_fts WHERE memories_fts MATCH ?",
                ("蓝莓蛋糕",),
            ).fetchone()[0],
            0,
        )


class NaturalLanguageRecallTests(unittest.TestCase):
    """Recall has to survive how people actually type.

    Regression: the substring fallback AND-ed its probe words, so a memory only
    matched if it literally contained *every* word of the question. Combined
    with a trigram index that skips two-character Chinese words, whole-sentence
    queries — which is all of them, since Chinese has no spaces — recalled
    nothing. The feature was effectively dead in production while every unit
    test passed, because those queried with the memory's own keywords.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = SqliteDatabase(Path(self.temp.name) / "bot.sqlite3")
        self.store = MemoryStore(self.database)
        self.store.observe(SCOPE, "7", "小明在备考计算机统考")
        self.store.observe(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        self.store.observe(SCOPE, "9", "小红养了一只叫豆豆的猫")

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def contents(self, query: str, limit: int = 4) -> set[str]:
        return {
            row["content"] for row in self.store.search_memories(SCOPE, query, limit)
        }

    def test_a_whole_sentence_finds_its_topic(self):
        self.assertIn("小明在备考计算机统考", self.contents("这周备考情况如何"))

    def test_an_unspaced_sentence_finds_its_topic(self):
        self.assertIn("小红养了一只叫豆豆的猫", self.contents("小红家那只猫叫什么来着"))

    def test_a_two_character_word_is_searchable(self):
        # 备考 is two characters: the trigram index cannot serve it.
        self.assertIn("小明在备考计算机统考", self.contents("备考"))

    def test_a_question_may_match_several_memories(self):
        found = self.contents("小明最近在忙什么")
        self.assertIn("小明在备考计算机统考", found)
        self.assertIn("小明喜欢蓝莓蛋糕", found)

    def test_an_unrelated_message_recalls_nothing(self):
        self.assertEqual(self.contents("今天天气不错"), set())

    def test_a_bare_greeting_recalls_nothing(self):
        self.assertEqual(self.contents("你好"), set())

    def test_more_overlapping_words_ranks_higher(self):
        rows = self.store.search_memories(SCOPE, "小明 蓝莓 蛋糕", 4)
        self.assertEqual(rows[0]["content"], "小明喜欢蓝莓蛋糕")

    def test_the_probe_list_is_deterministic(self):
        first = MemoryStore._tokens("这周备考情况如何")
        self.assertEqual(first, MemoryStore._tokens("这周备考情况如何"))
        self.assertTrue(first)
        self.assertTrue(all(len(token) <= 4 for token in first))


class MemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")
        self.persona = self.root / "persona.md"
        self.persona.write_text("固定人格", encoding="utf-8")
        self.service = MemoryService(
            FakeModel(), self.store, self.store, embeddings=False
        )

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def event(self, text: str, user_id: str = "7", event_id: str = "e1") -> MessageEvent:
        return MessageEvent(event_id, SCOPE, "42", user_id, "小明", text, (), True, (), 0)

    # -------------------------------------------------------------- injection

    def test_related_reports_source_and_reasons(self):
        self.service.remember(
            SCOPE, "7", "小明喜欢蓝莓蛋糕",
            source_event_id="m1", visibility="group",
        )
        hits = self.service.recall(SCOPE, "蓝莓蛋糕", subject_user_id="7")
        self.assertEqual(len(hits), 1)
        self.assertIsInstance(hits[0], RetrievalHit)
        self.assertEqual(hits[0].item.source_event_id, "m1")
        self.assertTrue(any("lexical=" in reason for reason in hits[0].reasons))
        self.assertIn("from m1", hits[0].explain())
        self.assertEqual(self.service.related(SCOPE, "蓝莓蛋糕"), ["小明喜欢蓝莓蛋糕"])

    def test_related_reinforces_what_it_injects(self):
        self.service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕", visibility="group")
        self.service.related(SCOPE, "蓝莓蛋糕")
        row = self.store.list_memories(SCOPE)[0]
        self.assertEqual(row["access_count"], 1)

    def test_related_focus_comes_from_the_latest_message(self):
        self.store.add_message("e1", SCOPE, "7", "小明", "user", "你好")
        self.service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕", visibility="personal")
        self.service.remember(SCOPE, "8", "小红喜欢草莓", visibility="personal")
        self.assertEqual(self.service.related(SCOPE, "喜欢"), ["小明喜欢蓝莓蛋糕"])
        self.store.add_message("e2", SCOPE, "8", "小红", "user", "在吗")
        self.assertEqual(self.service.related(SCOPE, "喜欢"), ["小红喜欢草莓"])

    def test_related_never_raises(self):
        broken = MemoryService(FakeModel(), self.store, object(), embeddings=False)
        with self.assertLogs("qunbot.memory.service", level="ERROR"):
            self.assertEqual(broken.related(SCOPE, "蓝莓蛋糕"), [])

    def test_other_scopes_are_never_injected(self):
        self.service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕", visibility="group")
        self.assertEqual(self.service.related(OTHER_SCOPE, "蓝莓蛋糕"), [])

    # ------------------------------------------------------------- write path

    def test_paraphrase_reinforces_instead_of_stacking(self):
        first = self.service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        second = self.service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕和奶油")
        self.assertEqual(first["outcome"], "inserted")
        self.assertEqual(second["outcome"], "reinforced")
        self.assertEqual(second["memory_id"], first["memory_id"])
        rows = self.store.list_memories(SCOPE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "小明喜欢蓝莓蛋糕")

    def test_contradiction_supersedes_the_old_fact(self):
        self.store.add_message("e1", SCOPE, "7", "小明", "user", "蓝莓蛋糕好吃吗")
        self.service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        outcome = self.service.remember(SCOPE, "7", "小明不喜欢蓝莓蛋糕")
        self.assertEqual(outcome["outcome"], "superseded")
        statuses = {row["content"]: row["status"] for row in self.store.list_memories(SCOPE)}
        self.assertEqual(statuses["小明喜欢蓝莓蛋糕"], "superseded")
        self.assertEqual(statuses["小明不喜欢蓝莓蛋糕"], "active")
        injected = self.service.related(SCOPE, "蓝莓蛋糕")
        self.assertEqual(injected, ["小明不喜欢蓝莓蛋糕"])

    def test_explicit_forget_api(self):
        memory_id = self.service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕")["memory_id"]
        self.assertEqual(self.service.forget_memory(memory_id), 1)
        self.assertEqual(self.service.stats(SCOPE)["total"], 0)
        self.service.remember(SCOPE, "8", "小红喜欢草莓")
        self.assertEqual(self.service.forget_scope(SCOPE), 1)

    # ------------------------------------------------------------ distillation

    def test_extract_stores_facts_with_source_event(self):
        self.store.add_message("m1", SCOPE, "7", "小明", "user", "我喜欢蓝莓蛋糕")
        self.store.add_message("m2", SCOPE, "7", "小明", "user", "最近在准备考研")
        model = FakeModel(
            json.dumps(
                [
                    {
                        "user_id": "7",
                        "fact": "小明喜欢蓝莓蛋糕",
                        "fact_type": "preference",
                        "confidence": 0.8,
                    },
                    {"user_id": "999", "fact": "陌生人说了什么", "confidence": 0.9},
                    {"user_id": "7", "fact": "短", "confidence": 0.9},
                ],
                ensure_ascii=False,
            )
        )
        service = MemoryService(model, self.store, self.store, embeddings=False)
        asyncio.run(service.extract(SCOPE))
        rows = self.store.list_memories(SCOPE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "小明喜欢蓝莓蛋糕")
        self.assertEqual(rows[0]["source_event_id"], "m2")
        self.assertEqual(rows[0]["fact_type"], "preference")
        self.assertEqual(rows[0]["visibility"], "personal")
        self.assertEqual(rows[0]["user_id"], "7")

    def test_extract_marks_group_level_facts_shared(self):
        self.store.add_message("m1", SCOPE, "7", "小明", "user", "每周五开黑")
        model = FakeModel(
            json.dumps(
                [{"user_id": "_group_", "fact": "群里每周五开黑", "confidence": 0.9}],
                ensure_ascii=False,
            )
        )
        service = MemoryService(model, self.store, self.store, embeddings=False)
        asyncio.run(service.extract(SCOPE))
        row = self.store.list_memories(SCOPE)[0]
        self.assertEqual(row["user_id"], "_group_")
        self.assertEqual(row["visibility"], "group")

    def test_extract_ignores_unparsable_model_output(self):
        self.store.add_message("m1", SCOPE, "7", "小明", "user", "你好")
        service = MemoryService(
            FakeModel("我觉得他们可能喜欢蛋糕"), self.store, self.store, embeddings=False
        )
        asyncio.run(service.extract(SCOPE))
        self.assertEqual(self.store.list_memories(SCOPE), [])

    def test_extract_low_confidence_becomes_candidate(self):
        self.store.add_message("m1", SCOPE, "7", "小明", "user", "我好像要去北京")
        model = FakeModel(
            json.dumps(
                [{"user_id": "7", "fact": "小明好像要去北京", "confidence": 0.3}],
                ensure_ascii=False,
            )
        )
        service = MemoryService(model, self.store, self.store, embeddings=False)
        asyncio.run(service.extract(SCOPE))
        row = self.store.list_memories(SCOPE)[0]
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(service.related(SCOPE, "去北京"), [])

    # ------------------------------------------------------------- maintenace

    def test_maintain_is_throttled(self):
        self.service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕")
        self.assertTrue(self.service.maintain(force=True) != {})
        self.assertEqual(self.service.maintain(), {})

    def test_model_extras_are_bounded(self):
        item = MemoryItem.from_row(
            {
                "id": 1,
                "scope": SCOPE,
                "user_id": "7",
                "content": "小明喜欢蓝莓蛋糕",
                "confidence": 0.7,
                "importance": 3,
                "status": "active",
                "visibility": "personal",
                "source_event_id": "m1",
                "created_at": 10,
            }
        )
        self.assertTrue(item.is_personal())
        self.assertIn("source_event=m1", item.describe())


class StablePrefixTests(unittest.TestCase):
    """Memories are dynamic data: they may never reach the cached prefix."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")
        self.persona = self.root / "persona.md"
        self.persona.write_text("固定人格", encoding="utf-8")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def agent(self, service: MemoryService) -> Agent:
        return Agent(
            FakeModel(),
            self.store,
            self.store,
            service,
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )

    def test_memory_updates_do_not_touch_the_stable_prefix(self):
        service = MemoryService(FakeModel(), self.store, self.store, embeddings=False)
        agent = self.agent(service)
        before = agent.stable_prefix()
        service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕", visibility="group")
        service.remember(SCOPE, "7", "小明在准备考研", visibility="group")
        self.assertEqual(before, agent.stable_prefix())
        self.assertEqual(agent.stable_prefix().encode(), before.encode())

    def test_memories_land_in_the_dynamic_suffix_only(self):
        service = MemoryService(FakeModel(), self.store, self.store, embeddings=False)
        service.remember(SCOPE, "7", "小明喜欢蓝莓蛋糕", visibility="group")
        agent = self.agent(service)
        messages = agent.build_messages(
            MessageEvent("e1", SCOPE, "42", "7", "小明", "蓝莓蛋糕", (), True, (), 0)
        )
        self.assertEqual(messages[0]["content"], agent.stable_prefix())
        self.assertNotIn("蓝莓蛋糕", messages[0]["content"])
        self.assertIn("相关记忆", messages[-1]["content"])
        self.assertIn("小明喜欢蓝莓蛋糕", messages[-1]["content"])

    def test_focus_subject_is_read_from_the_conversation_repository(self):
        self.store.add_message("e1", SCOPE, "7", "小明", "user", "蓝莓蛋糕")
        service = MemoryService(FakeModel(), self.store, self.store, embeddings=False)
        self.assertEqual(service._focus_subject(SCOPE), "7")
        self.assertEqual(service._focus_subject(OTHER_SCOPE), None)


if __name__ == "__main__":
    unittest.main()
