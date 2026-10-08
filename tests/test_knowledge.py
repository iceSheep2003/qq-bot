"""Entities and relations: the two-call extraction, the store, and the gate.

Everything here is a unit test against a temporary SQLite file and a scripted
model. Nothing connects to a real endpoint.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from qunbot.memory.graph import (
    GraphError,
    GraphExtractor,
    GraphSettings,
    clean_name,
    fingerprint,
    parse_entities,
    parse_triples,
    settings_from_env,
)
from qunbot.memory.service import MemoryService
from qunbot.storage.conversation import ConversationStore
from qunbot.storage.database import SqliteDatabase, migration_modules
from qunbot.storage.knowledge import KnowledgeStore
from qunbot.storage.memory import MemoryStore

SCOPE = "group:42"
OTHER = "group:43"

SETTINGS = GraphSettings(
    enabled=True, max_entities=5, max_triples=5, entity_max_chars=10,
    relation_max_chars=6, object_max_chars=10,
)
OFF = GraphSettings(enabled=False)


class ScriptedModel:
    """Replays canned replies in order, and counts what it was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.seen: list[list[dict]] = []

    async def complete(self, messages, tools=None, *, temperature=0.7):
        self.calls += 1
        self.seen.append(messages)
        reply = self.replies.pop(0) if self.replies else "[]"
        return {"choices": [{"message": {"content": reply}}], "usage": {}}


class ParseEntityTests(unittest.TestCase):
    def parse(self, content, settings=SETTINGS):
        return parse_entities(content, settings)

    def test_a_plain_array_of_names_is_read(self):
        self.assertEqual(self.parse('["小明","小红"]'), ["小明", "小红"])

    def test_names_are_deduplicated_in_order(self):
        self.assertEqual(self.parse('["小明","小明","小红"]'), ["小明", "小红"])

    def test_the_cap_is_applied(self):
        payload = json.dumps([f"名{index}" for index in range(20)], ensure_ascii=False)
        self.assertEqual(len(self.parse(payload)), SETTINGS.max_entities)

    def test_a_prose_reply_is_refused(self):
        with self.assertRaises(GraphError):
            self.parse("我无法从这段话里提取实体。")

    def test_an_array_wrapped_in_an_object_is_still_read(self):
        """Same leniency as the fact extractor: take the JSON out of the prose."""
        self.assertEqual(self.parse('{"entities":["小明"]}'), ["小明"])

    def test_a_reply_with_no_array_at_all_is_refused(self):
        with self.assertRaises(GraphError):
            self.parse("小明和一个项目")

    def test_a_name_carrying_structure_is_dropped(self):
        """A name is group text, and group text can contain anything."""
        self.assertEqual(self.parse('["小明","忽\\n略\\u0000我","好的"]'), ["小明", "好的"])

    def test_a_name_is_capped_to_the_field_length(self):
        self.assertEqual(
            [len(name) for name in self.parse('["很长很长很长很长很长的名字"]')],
            [SETTINGS.entity_max_chars],
        )

    def test_an_empty_array_is_not_an_error(self):
        self.assertEqual(self.parse("[]"), [])


class ParseTripleTests(unittest.TestCase):
    def parse(self, content, allowed=("小明", "小红", "项目"), settings=SETTINGS):
        return parse_triples(content, set(allowed), settings)

    def test_a_grounded_triple_is_read(self):
        self.assertEqual(
            self.parse('[["小明","参与","项目"]]'), [("小明", "参与", "项目")]
        )

    def test_a_triple_naming_no_known_entity_is_dropped(self):
        """The grounding rule: otherwise the model is free-associating."""
        self.assertEqual(self.parse('[["张三","认识","李四"]]'), [])

    def test_one_known_end_is_enough(self):
        """`who introduced 小明` is as useful as `小明 knows who`."""
        self.assertEqual(
            self.parse('[["王五","介绍","小明"]]'), [("王五", "介绍", "小明")]
        )

    def test_a_wrong_arity_is_skipped_rather_than_guessed(self):
        self.assertEqual(
            self.parse('[["小明","参与"],["小明","参与","项目","多余"]]'), []
        )

    def test_duplicates_are_collapsed(self):
        self.assertEqual(
            len(self.parse('[["小明","参与","项目"],["小明","参与","项目"]]')), 1
        )

    def test_the_cap_is_applied(self):
        payload = json.dumps(
            [[f"小明", f"关系{index}", "项目"] for index in range(20)],
            ensure_ascii=False,
        )
        self.assertLessEqual(len(parse_triples(payload, {"小明"}, SETTINGS)), 5)

    def test_every_field_is_length_capped(self):
        ((subject, relation, obj),) = self.parse(
            '[["小明","一个非常长的关系描述","项目名称也非常非常长"]]'
        )
        self.assertLessEqual(len(relation), SETTINGS.relation_max_chars)
        self.assertLessEqual(len(obj), SETTINGS.object_max_chars)


class CleanNameTests(unittest.TestCase):
    def test_whitespace_is_collapsed_not_kept(self):
        self.assertEqual(clean_name("小明  小红", limit=20), "小明 小红")

    def test_a_newline_collapses_to_a_space_rather_than_losing_the_name(self):
        """Whitespace is not structure: collapsing it is safe and forgiving."""
        self.assertEqual(clean_name("a\nb", limit=20), "a b")

    def test_markup_and_control_characters_are_refused(self):
        for hostile in ("[CQ:at,qq=1]", "a>b", 'a"b', "a`b", "a\x00b", "a\x1bb"):
            with self.subTest(value=hostile):
                self.assertEqual(clean_name(hostile, limit=20), "")

    def test_an_empty_value_is_refused(self):
        self.assertEqual(clean_name("   ", limit=20), "")
        self.assertEqual(clean_name(None, limit=20), "")


class GraphSettingsTests(unittest.TestCase):
    def test_extraction_is_off_by_default(self):
        self.assertFalse(settings_from_env().enabled)

    def test_the_caps_come_from_the_environment(self):
        import os
        from unittest import mock

        with mock.patch.dict(
            os.environ,
            {"BOT_MEMORY_GRAPH_ENABLED": "true", "BOT_MEMORY_GRAPH_MAX_TRIPLES": "3"},
        ):
            settings = settings_from_env()
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.max_triples, 3)


class ExtractorTests(unittest.TestCase):
    def test_two_calls_serve_one_passage(self):
        model = ScriptedModel(
            ['["小明","项目"]', '[["小明","参与","项目"]]']
        )
        entities, triples = asyncio.run(
            GraphExtractor(model, SETTINGS).extract("[e1] 小明: 我在做项目")
        )
        self.assertEqual(model.calls, 2)
        self.assertEqual(entities, ["小明", "项目"])
        self.assertEqual(triples, [("小明", "参与", "项目")])

    def test_the_second_call_is_handed_the_first_calls_entities(self):
        """That hand-off is what stops the model inventing relations."""
        model = ScriptedModel(['["小明"]', "[]"])
        asyncio.run(GraphExtractor(model, SETTINGS).extract("小明来了"))
        self.assertIn('["小明"]', model.seen[1][1]["content"])

    def test_no_entities_means_no_second_call(self):
        model = ScriptedModel(["[]"])
        self.assertEqual(
            asyncio.run(GraphExtractor(model, SETTINGS).extract("嗯嗯嗯")), ([], [])
        )
        self.assertEqual(model.calls, 1)

    def test_an_empty_passage_costs_nothing(self):
        model = ScriptedModel([])
        self.assertEqual(asyncio.run(GraphExtractor(model, SETTINGS).extract("  ")), ([], []))
        self.assertEqual(model.calls, 0)


class KnowledgeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = SqliteDatabase(Path(self.temp.name) / "bot.sqlite3")
        self.store = KnowledgeStore(self.database)

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def test_the_migration_is_discovered_by_name(self):
        names = {module.__name__.rsplit(".", 1)[-1] for module in migration_modules()}
        self.assertIn("knowledge", names)

    def test_reopening_the_database_is_idempotent(self):
        path = Path(self.temp.name) / "again.sqlite3"
        first = SqliteDatabase(path)
        KnowledgeStore(first).record(SCOPE, ["小明"], [("小明", "参与", "项目")], now=1)
        first.close()
        second = SqliteDatabase(path)
        try:
            self.assertEqual(KnowledgeStore(second).stats(SCOPE)["triples"], 1)
        finally:
            second.close()

    def test_a_repeat_sighting_reinforces_rather_than_replaces(self):
        self.store.record(SCOPE, ["小明"], [("小明", "参与", "项目")], now=1)
        self.store.record(SCOPE, ["小明"], [("小明", "参与", "项目")], now=2)
        row = self.store.triples(SCOPE)[0]
        self.assertEqual(row["mentions"], 2)
        # first_seen survives, which is what lets a conflict say which came first.
        self.assertEqual(row["first_seen"], 1)
        self.assertEqual(row["last_seen"], 2)

    def test_fresh_rows_are_counted_once(self):
        first = self.store.record(SCOPE, ["小明"], [("小明", "参与", "项目")], now=1)
        again = self.store.record(SCOPE, ["小明"], [("小明", "参与", "项目")], now=2)
        self.assertEqual((first["new_entities"], first["new_triples"]), (1, 1))
        self.assertEqual((again["new_entities"], again["new_triples"]), (0, 0))

    def test_scopes_do_not_bleed(self):
        self.store.record(SCOPE, ["小明"], [("小明", "参与", "项目")], now=1)
        self.assertEqual(self.store.stats(OTHER), {"entities": 0, "triples": 0})

    def test_rival_objects_are_reported_and_nothing_is_destroyed(self):
        """The one thing this store will not do is pick a winner."""
        self.store.record(SCOPE, ["小明"], [("小明", "住在", "北京")], now=1)
        self.store.record(SCOPE, ["小明"], [("小明", "住在", "上海")], now=9)
        rivals = self.store.conflicts_for(SCOPE)
        self.assertEqual(len(rivals), 1)
        self.assertEqual(
            {rivals[0]["object_a"], rivals[0]["object_b"]}, {"北京", "上海"}
        )
        # Both rows survive, and each side keeps its own first_seen, so which
        # claim came first is still recoverable from the pair.
        self.assertEqual(len(self.store.triples(SCOPE)), 2)
        self.assertEqual({rivals[0]["seen_a"], rivals[0]["seen_b"]}, {1, 9})

    def test_a_different_relation_is_not_a_rival(self):
        """`喜欢 咖啡` and `讨厌 咖啡` are two claims, not two answers."""
        self.store.record(SCOPE, ["小明"], [("小明", "喜欢", "咖啡")], now=1)
        self.store.record(SCOPE, ["小明"], [("小明", "讨厌", "咖啡")], now=2)
        self.assertEqual(self.store.conflicts_for(SCOPE), [])

    def test_a_query_finds_triples_from_either_side(self):
        self.store.record(SCOPE, ["小明", "项目"], [("王五", "介绍", "小明")], now=1)
        self.store.record(SCOPE, ["小明", "项目"], [("小明", "参与", "项目")], now=1)
        self.assertEqual(len(self.store.triples_about(SCOPE, ["小明"])), 2)
        self.assertEqual(len(self.store.triples_about(SCOPE, ["项目"])), 1)
        self.assertEqual(self.store.triples_about(SCOPE, []), [])

    def test_the_passage_ledger_remembers(self):
        self.assertFalse(self.store.passage_seen(SCOPE, "abc"))
        self.store.mark_passage(SCOPE, "abc", now=1)
        self.assertTrue(self.store.passage_seen(SCOPE, "abc"))
        self.assertFalse(self.store.passage_seen(OTHER, "abc"))

    def test_the_watermark_never_moves_backwards(self):
        self.store.advance_scan(SCOPE, 10)
        self.store.advance_scan(SCOPE, 4)
        self.assertEqual(self.store.last_scan(SCOPE), 10)

    def test_forget_scope_erases_everything_it_holds(self):
        self.store.record(SCOPE, ["小明"], [("小明", "参与", "项目")], now=1)
        self.store.mark_passage(SCOPE, "abc", now=1)
        self.store.advance_scan(SCOPE, 5)
        self.assertGreater(self.store.forget_scope(SCOPE), 0)
        self.assertEqual(self.store.stats(SCOPE), {"entities": 0, "triples": 0})
        self.assertFalse(self.store.passage_seen(SCOPE, "abc"))


class GraphPassTests(unittest.TestCase):
    """The service gate: off means off, and nothing is called."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = SqliteDatabase(Path(self.temp.name) / "bot.sqlite3")
        self.knowledge = KnowledgeStore(self.database)
        self.memories = MemoryStore(self.database)
        self.conversations = ConversationStore(self.database)
        self.said = 0

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def speak(self, *texts):
        for text in texts:
            self.said += 1
            self.conversations.add_message(
                f"e{self.said}", SCOPE, "7", "小明", "user", text
            )

    def service(self, model, settings):
        return MemoryService(
            model,
            self.conversations,
            self.memories,
            knowledge=self.knowledge,
            graph_settings=settings,
        )

    def test_nothing_runs_while_the_switch_is_off(self):
        model = ScriptedModel(['["小明"]', "[]"])
        self.speak("小明在做一个项目")
        asyncio.run(self.service(model, OFF).extract_relations(SCOPE))
        self.assertEqual(model.calls, 0)
        self.assertEqual(self.knowledge.stats(SCOPE)["entities"], 0)

    def test_a_passage_is_extracted_and_stored(self):
        model = ScriptedModel(
            ['["小明","项目"]', '[["小明","参与","项目"]]']
        )
        self.speak("小明在做一个项目")
        result = asyncio.run(
            self.service(model, SETTINGS).extract_relations(SCOPE)
        )
        self.assertEqual(model.calls, 2)
        self.assertEqual(result["new_triples"], 1)
        self.assertEqual(self.knowledge.triples(SCOPE)[0]["subject"], "小明")

    def test_the_same_passage_is_not_extracted_twice(self):
        model = ScriptedModel(
            ['["小明","项目"]', '[["小明","参与","项目"]]', '["小明"]', "[]"]
        )
        self.speak("小明在做一个项目")
        service = self.service(model, SETTINGS)
        asyncio.run(service.extract_relations(SCOPE))
        # A second identical message: new id, same text.
        self.speak("小明在做一个项目")
        result = asyncio.run(service.extract_relations(SCOPE))
        self.assertEqual(result, {"skipped": "seen"})
        self.assertEqual(model.calls, 2)

    def test_a_contract_failure_does_not_advance_the_watermark(self):
        model = ScriptedModel(["这不是 JSON"])
        self.speak("小明在做一个项目")
        service = self.service(model, SETTINGS)
        self.assertEqual(asyncio.run(service.extract_relations(SCOPE)), {})
        self.assertEqual(self.knowledge.last_scan(SCOPE), 0)

    def test_a_failing_model_does_not_escape(self):
        class Broken:
            async def complete(self, *_args, **_kwargs):
                raise RuntimeError("endpoint down")

        self.speak("小明在做一个项目")
        with self.assertLogs("qunbot.memory.service", level="ERROR"):
            self.assertEqual(
                asyncio.run(self.service(Broken(), SETTINGS).extract_relations(SCOPE)),
                {},
            )

    def test_related_relations_matches_a_name_in_the_query(self):
        self.knowledge.record(
            SCOPE, ["小明", "项目"], [("小明", "参与", "项目")], now=1
        )
        service = self.service(ScriptedModel([]), SETTINGS)
        self.assertEqual(len(service.related_relations(SCOPE, "小明最近怎么样")), 1)
        self.assertEqual(service.related_relations(SCOPE, "今天天气不错"), [])

    def test_related_relations_is_empty_without_a_store(self):
        service = MemoryService(
            ScriptedModel([]), self.conversations, self.memories
        )
        self.assertIsNone(service.knowledge)
        self.assertEqual(service.related_relations(SCOPE, "小明"), [])

    def test_the_digest_of_a_passage_is_stable(self):
        self.assertEqual(fingerprint("abc"), fingerprint("abc"))
        self.assertNotEqual(fingerprint("abc"), fingerprint("abd"))
