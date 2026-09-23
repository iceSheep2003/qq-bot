"""Self Learning: discovery, confidence, review, and bounded use.

The four stages are tested separately because they are the feature. The
boundary tests at the bottom are the ones that matter most: this package reads
group messages and must not, under any configuration, write affection or
memory, touch the persona file, or reach the stable prompt prefix.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from qunbot.domain import MessageEvent
from qunbot.extensions import slang as slang_module
from qunbot.extensions.slang import (
    CONTEXT_MAX_CHARS,
    CONTEXT_PRIORITY,
    SlangConfig,
    SlangWorker,
    TermIndex,
    bind_service,
    register,
    render_terms,
    validate,
)
from qunbot.extensions.slang import mining
from qunbot.extensions.slang.review import (
    Review,
    ReviewFile,
    load_review,
    parse_review,
)
from qunbot.runtime.agent import Agent
from qunbot.runtime.context import ContextRegistry, Trust
from qunbot.runtime.service import BotPolicy, ConversationService
from qunbot.runtime.skills import SkillCatalog
from qunbot.runtime.tools import built_in_tools
from qunbot.memory.service import MemoryService
from qunbot.storage.conversation import ConversationStore
from qunbot.storage.database import SqliteDatabase, migration_modules
from qunbot.storage.slang import SlangStore
from support import Store

GROUP = "group:42"

# "上大分" is the invented one; 今天/什么/我们 are ordinary language and must be
# filtered out by the word list rather than promoted to candidates.
BREAKOUT = "上大分"
CHATTER = [
    "今天上大分了吗",
    "上大分上大分",
    "我们上大分了",
    "上大分真爽",
    "什么叫做上大分啊",
]


class FakeModel:
    async def complete(self, messages, tools=None, *, temperature=0.7):
        return {
            "choices": [{"message": {"content": "收到。"}}],
            "usage": {"prompt_tokens": 10},
        }


def event(scope: str = GROUP, text: str = "你好") -> MessageEvent:
    group = scope.split(":", 1)[1] if scope.startswith("group:") else None
    return MessageEvent("e1", scope, group, "7", "小明", text, (), True, (), 0)


class SlangTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")
        self.slang = SlangStore(self.store)
        self.config = SlangConfig(enabled=True)
        self.worker = SlangWorker(
            self.slang, self.config, [GROUP], self.slang.recent_messages
        )

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def host(self):
        return SimpleNamespace(
            context=ContextRegistry(), workers=[],
            closers=[],
            tools=None,
            binders=[],
        )

    def app_config(self, groups=("42",)):
        return SimpleNamespace(
            db_path=self.root / "wired.sqlite3", group_allowlist=frozenset(groups)
        )

    def speak(self, texts, *, scope: str = GROUP, role: str = "user"):
        for index, text in enumerate(texts):
            self.store.conversations.add_message(
                f"{scope}-{role}-{index}",
                scope,
                str(index % 3),
                f"u{index}",
                role,
                text,
            )

    def learn(self, texts=None, *, scope: str = GROUP, **overrides):
        self.speak(texts if texts is not None else CHATTER, scope=scope)
        if overrides:
            self.worker = SlangWorker(
                self.slang,
                replace(self.config, **overrides),
                [scope],
                self.slang.recent_messages,
            )
        return self.worker.scan(scope)

    def review(self, payload, *, scopes=(GROUP,)):
        path = self.root / "slang_review.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        worker = SlangWorker(
            self.slang,
            replace(self.config, review_path=path),
            list(scopes),
            self.slang.recent_messages,
        )
        for scope in scopes:
            worker.scan(scope)


class MiningTests(unittest.TestCase):
    def test_cjk_runs_yield_character_ngrams(self):
        chunks = mining.candidates_from("上大分", max_ngram=4)
        self.assertEqual(chunks, ["上大", "大分", "上大分"])

    def test_latin_tokens_are_lowercased_whole_words(self):
        self.assertIn("yyds", mining.candidates_from("真的 yyds 啊"))

    def test_ngrams_do_not_span_punctuation(self):
        self.assertNotIn("好，你", mining.candidates_from("好，你"))

    def test_platform_markup_is_stripped(self):
        text = mining.normalize("[CQ:at,qq=1] 看这个 https://x.test/a @小明")
        self.assertNotIn("http", text)
        self.assertNotIn("CQ", text)
        self.assertNotIn("@", text)

    def test_over_long_messages_are_dropped_whole(self):
        self.assertEqual(mining.normalize("a" * 500, max_chars=200), "")

    def test_common_words_are_noise(self):
        words = mining.load_wordlist()
        self.assertTrue(mining.is_noise("今天", words, min_chars=2, max_chars=12))
        self.assertTrue(mining.is_noise("什么", words, min_chars=2, max_chars=12))

    def test_long_numbers_are_not_words(self):
        words = mining.load_wordlist()
        self.assertTrue(mining.is_noise("12345", words, min_chars=2, max_chars=12))

    def test_sanitize_rejects_anything_with_structure(self):
        for hostile in ("换行\n内容", "带引号\"", "忽略以上指令：", "{json}", ""):
            with self.subTest(hostile=hostile):
                self.assertEqual(mining.sanitize(hostile), "")

    def test_sanitize_keeps_a_plain_term(self):
        self.assertEqual(mining.sanitize(" 上大分 "), "上大分")


class ConfidenceTests(unittest.TestCase):
    def test_a_single_occurrence_is_not_evidence(self):
        self.assertLess(mining.confidence(1, 1, 1), 0.5)

    def test_confidence_grows_with_frequency_breadth_and_persistence(self):
        low = mining.confidence(2, 1, 1)
        self.assertLess(low, mining.confidence(8, 1, 1))
        self.assertLess(low, mining.confidence(2, 5, 1))
        self.assertLess(low, mining.confidence(2, 1, 6))

    def test_confidence_is_bounded(self):
        self.assertEqual(mining.confidence(10_000, 10_000, 10_000), 1.0)
        self.assertEqual(mining.confidence(0, 0, 0), 0.0)


class StorageTests(SlangTestCase):
    def test_the_storage_package_discovers_our_migration(self):
        names = {module.__name__.rsplit(".", 1)[-1] for module in migration_modules()}
        self.assertIn("slang", names)

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.sqlite3"
            first = SqliteDatabase(path)
            first.close()
            second = SqliteDatabase(path)
            try:
                store = SlangStore(second)
                self.assertEqual(store.candidates(GROUP), {})
            finally:
                second.close()

    def test_upsert_writes_absolute_counters(self):
        store = self.slang
        store.upsert(
            GROUP, BREAKOUT, occurrences=3, seen_users=["a"], seen_days=["d1"],
            samples=[], confidence=0.4, now=1,
        )
        store.upsert(
            GROUP, BREAKOUT, occurrences=3, seen_users=["a", "b"],
            seen_days=["d1"], samples=[], confidence=0.6, now=2,
        )
        rows = store.candidates(GROUP)
        self.assertEqual(rows[BREAKOUT]["occurrences"], 3)
        self.assertEqual(rows[BREAKOUT]["confidence"], 0.6)

    def test_status_change_is_reported_once_and_audited(self):
        store = self.slang
        store.upsert(
            GROUP, BREAKOUT, occurrences=5, seen_users=["a"], seen_days=["d1"],
            samples=[], confidence=0.9, now=1,
        )
        self.assertTrue(store.set_status(GROUP, BREAKOUT, "approved"))
        self.assertFalse(store.set_status(GROUP, BREAKOUT, "approved"))
        store.log_review(GROUP, BREAKOUT, "approve", "sounds useful")
        self.assertEqual(store.reviews(GROUP)[0]["action"], "approve")

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            self.slang.set_status(GROUP, BREAKOUT, "trusted")

    def test_reviewed_rows_survive_pruning(self):
        store = self.slang
        for index, term in enumerate(["aa", "bb", "cc"]):
            store.upsert(
                GROUP, term, occurrences=2, seen_users=["a"], seen_days=["d1"],
                samples=[], confidence=0.1 * index, now=1,
            )
        store.set_status(GROUP, "aa", "approved")  # lowest confidence
        store.prune(GROUP, keep=1)
        remaining = set(store.candidates(GROUP))
        self.assertEqual(remaining, {"aa", "cc"})

    def test_watermark_never_moves_backwards(self):
        self.slang.advance_scan(GROUP, 10)
        self.slang.advance_scan(GROUP, 3)
        self.assertEqual(self.slang.last_scan(GROUP), 10)

    def test_forget_removes_only_its_own_scope(self):
        store = self.slang
        for scope in (GROUP, "group:43"):
            store.upsert(
                scope, BREAKOUT, occurrences=3, seen_users=["a"], seen_days=["d1"],
                samples=[], confidence=0.9, now=1,
            )
        store.forget(GROUP)
        self.assertEqual(store.candidates(GROUP), {})
        self.assertIn(BREAKOUT, store.candidates("group:43"))


class DiscoveryTests(SlangTestCase):
    def test_repeated_group_phrase_becomes_a_candidate(self):
        self.learn()
        self.assertIn(BREAKOUT, self.slang.candidates(GROUP))

    def test_ordinary_vocabulary_is_not_a_candidate(self):
        self.learn()
        found = set(self.slang.candidates(GROUP))
        for ordinary in ("今天", "什么", "我们"):
            self.assertNotIn(ordinary, found)

    def test_bot_messages_are_not_learned(self):
        self.speak([BREAKOUT] * 6, role="assistant")
        self.worker.scan(GROUP)
        self.assertEqual(self.slang.candidates(GROUP), {})

    def test_candidates_carry_evidence(self):
        self.learn()
        row = self.slang.candidates(GROUP)[BREAKOUT]
        self.assertGreaterEqual(row["occurrences"], 3)
        self.assertGreaterEqual(len(json.loads(row["seen_users"])), 2)
        self.assertTrue(json.loads(row["samples"]))
        self.assertGreater(row["confidence"], 0.0)

    def test_samples_are_bounded(self):
        self.learn()
        row = self.slang.candidates(GROUP)[BREAKOUT]
        self.assertLessEqual(len(json.loads(row["samples"])), self.config.max_samples)

    def test_a_single_occurrence_is_not_stored_by_default(self):
        self.speak(["这句话只出现过一次横竖撇捺"])
        self.worker.scan(GROUP)
        self.assertEqual(self.slang.candidates(GROUP), {})

    def test_singletons_can_be_recorded_when_a_deployer_asks(self):
        self.speak(["这句话只出现过一次横竖撇捺"])
        worker = SlangWorker(
            self.slang,
            replace(self.config, min_occurrences=1),
            [GROUP],
            self.slang.recent_messages,
        )
        worker.scan(GROUP)
        recorded = self.slang.candidates(GROUP)
        self.assertTrue(recorded)
        for row in recorded.values():
            with self.subTest(term=row["term"]):
                self.assertEqual(row["status"], "candidate")
                self.assertLess(row["confidence"], self.config.understand_confidence)

    def test_a_rescan_does_not_double_count(self):
        self.learn()
        before = self.slang.candidates(GROUP)[BREAKOUT]["occurrences"]
        too_fast = [
            row for row in self.slang.recent_messages(GROUP, 50) if row["role"] == "user"
        ]
        self.assertTrue(too_fast)
        self.worker.scan(GROUP)
        self.assertEqual(self.slang.candidates(GROUP)[BREAKOUT]["occurrences"], before)

    def test_scopes_do_not_bleed_into_each_other(self):
        self.learn(scope=GROUP)
        self.assertEqual(self.slang.candidates("group:43"), {})


class ReviewTests(SlangTestCase):
    def test_the_reviewer_can_approve_a_discovered_term(self):
        self.learn()
        self.review({"approve": [BREAKOUT]})
        row = self.slang.candidates(GROUP)[BREAKOUT]
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["origin"], "review")

    def test_the_reviewer_can_reject_a_term(self):
        self.learn()
        self.review({"reject": [BREAKOUT]})
        self.assertEqual(self.slang.candidates(GROUP)[BREAKOUT]["status"], "rejected")

    def test_a_two_scope_review_only_touches_its_scope(self):
        self.learn(scope=GROUP)
        self.learn(scope="group:43")
        self.review(
            {"scopes": {GROUP: {"approve": [BREAKOUT]}}}, scopes=(GROUP, "group:43")
        )
        self.assertEqual(self.slang.candidates(GROUP)[BREAKOUT]["status"], "approved")
        self.assertEqual(
            self.slang.candidates("group:43")[BREAKOUT]["status"], "candidate"
        )

    def test_reset_returns_a_term_to_candidate(self):
        self.learn()
        self.review({"approve": [BREAKOUT]})
        self.review({"reset": [BREAKOUT]})
        self.assertEqual(self.slang.candidates(GROUP)[BREAKOUT]["status"], "candidate")

    def test_review_decisions_are_audited(self):
        self.learn()
        self.review({"reject": [BREAKOUT]})
        self.assertEqual(self.slang.reviews(GROUP)[0]["action"], "reject")

    def test_an_edited_file_is_picked_up_by_the_next_scan(self):
        path = self.root / "slang_review.json"
        path.write_text(json.dumps({"approve": [BREAKOUT]}), encoding="utf-8")
        review = ReviewFile(path)
        self.assertEqual(review.current().status_for(GROUP, BREAKOUT), "approved")
        path.write_text(json.dumps({"reject": [BREAKOUT]}), encoding="utf-8")
        self.assertEqual(review.current().status_for(GROUP, BREAKOUT), "rejected")

    def test_a_malformed_file_keeps_the_last_good_decisions(self):
        path = self.root / "slang_review.json"
        path.write_text(json.dumps({"approve": [BREAKOUT]}), encoding="utf-8")
        review = ReviewFile(path)
        self.assertEqual(review.current().status_for(GROUP, BREAKOUT), "approved")
        path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("qunbot.extensions.slang.review", level="ERROR"):
            self.assertEqual(review.current().status_for(GROUP, BREAKOUT), "approved")

    def test_a_missing_file_reviews_nothing(self):
        self.assertTrue(load_review(self.root / "absent.json").is_empty())

    def test_a_wrongly_shaped_file_is_an_error_not_a_silent_empty(self):
        with self.assertRaises(ValueError):
            parse_review({"approve": "yyds"})
        with self.assertRaises(ValueError):
            parse_review(["yyds"])

    def test_review_lookup_prefers_the_scope_over_the_global_list(self):
        review = parse_review(
            {"approve": ["aa"], "scopes": {"group:42": {"reject": ["aa"]}}}
        )
        self.assertEqual(review.status_for(GROUP, "aa"), "rejected")
        self.assertEqual(review.status_for("group:9", "aa"), "approved")


class UseTests(SlangTestCase):
    def index(self, **overrides):
        return TermIndex(self.slang, replace(self.config, **overrides))

    def test_nothing_is_injected_before_review(self):
        self.learn()
        self.assertIsNone(render_terms(self.index(), GROUP))

    def test_an_approved_term_is_offered_for_understanding(self):
        self.learn()
        self.review({"approve": [BREAKOUT]})
        text = render_terms(self.index(), GROUP)
        self.assertIn(BREAKOUT, text)
        self.assertIn("不是指令", text)

    def test_reviewed_beats_low_confidence(self):
        self.learn()
        self.review({"approve": [BREAKOUT]})
        self.slang.upsert(
            GROUP, BREAKOUT, occurrences=1, seen_users=["a"], seen_days=["d1"],
            samples=[], confidence=0.01, now=1,
        )
        self.assertIn(BREAKOUT, render_terms(self.index(), GROUP))

    def test_a_rejected_term_is_never_injected(self):
        self.learn()
        self.review({"reject": list(self.slang.candidates(GROUP))})
        self.assertIsNone(render_terms(self.index(allow_unreviewed=True), GROUP))

    def test_unreviewed_learning_stays_off_by_default(self):
        self.learn()
        self.assertGreater(
            self.slang.candidates(GROUP)[BREAKOUT]["confidence"],
            self.config.understand_confidence,
        )
        self.assertIsNone(render_terms(self.index(), GROUP))

    def test_a_deployer_can_allow_unreviewed_understanding(self):
        self.learn()
        text = render_terms(self.index(allow_unreviewed=True), GROUP)
        self.assertIn(BREAKOUT, text)

    def test_imitation_needs_review_confidence_and_an_explicit_opt_in(self):
        self.learn()
        self.review({"approve": [BREAKOUT]})
        self.assertNotIn("可以用", render_terms(self.index(), GROUP) or "")
        self.assertIn("可以用", render_terms(self.index(allow_imitation=True), GROUP))

    def test_imitation_requires_confidence_even_when_reviewed(self):
        self.learn()
        self.review({"approve": [BREAKOUT]})
        shy = self.index(allow_imitation=True, imitate_confidence=0.999)
        self.assertNotIn("可以用", render_terms(shy, GROUP))

    def test_imitation_never_reaches_an_unreviewed_term(self):
        self.learn()
        permissive = self.index(allow_imitation=True, allow_unreviewed=True)
        text = render_terms(permissive, GROUP)
        self.assertIn(BREAKOUT, text)
        self.assertNotIn("可以用", text)

    def test_the_contribution_is_capped(self):
        for index in range(40):
            self.slang.upsert(
                GROUP, f"词{index}", occurrences=9, seen_users=["a"], seen_days=["d1"],
                samples=[], confidence=0.99, now=1,
            )
            self.slang.set_status(GROUP, f"词{index}", "approved")
        text = render_terms(self.index(max_terms=5), GROUP)
        self.assertEqual(text.count("、"), 4)

    def test_the_prompt_never_carries_raw_message_text(self):
        self.learn()
        self.review({"approve": [BREAKOUT]})
        text = render_terms(self.index(), GROUP)
        for row in self.slang.candidates(GROUP).values():
            for sample in json.loads(row["samples"]):
                self.assertNotIn(sample["text"], text)

    def test_scopes_do_not_share_terms(self):
        self.learn()
        self.review({"approve": [BREAKOUT]})
        self.assertIsNone(render_terms(self.index(), "group:43"))


class WiringTests(SlangTestCase):
    def test_disabled_by_default(self):
        self.assertFalse(SlangConfig.from_env().enabled)

    def test_a_disabled_extension_contributes_nothing(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"BOT_SLANG_ENABLED": "false"}):
            host = self.host()
            register(host, self.app_config(), FakeModel())
            self.assertEqual(host.context.names(), [])
            self.assertEqual(host.workers, [])
            self.assertEqual(validate(), {"enabled": False})

    def test_enabled_registration_is_derived_bounded_and_low_priority(self):
        import os
        from unittest import mock

        with mock.patch.dict(
            os.environ,
            {
                "BOT_SLANG_ENABLED": "true",
                "BOT_SLANG_REVIEW_PATH": str(self.root / "slang_review.json"),
            },
        ):
            host = self.host()
            register(host, self.app_config(), FakeModel())
            self.assertEqual(host.context.names(), ["group_slang"])
            registration = host.context._providers["group_slang"]
            self.assertEqual(registration.trust, Trust.DERIVED)
            self.assertEqual(registration.priority, CONTEXT_PRIORITY)
            self.assertLessEqual(registration.max_chars, CONTEXT_MAX_CHARS)
            self.assertLessEqual(CONTEXT_MAX_CHARS, 300)
            self.assertEqual(len(host.workers), 1)
            self.assertEqual(len(host.closers), 1)
            host.closers[0]()

    def test_low_priority_loses_the_budget_to_mood_and_history(self):
        # 70 is numerically larger than mood's 60, so a tight budget drops slang
        # first — it is the least load-bearing contribution in the suffix.
        self.assertGreater(70, 60)
        registry = ContextRegistry(budget_chars=40)
        registry.register("group_slang", lambda _e: "x" * 30, priority=70, max_chars=300)
        registry.register("mood", lambda _e: "y" * 30, priority=60, max_chars=200)
        collected = registry.collect(event())
        self.assertNotIn("group_slang", collected)
        self.assertIn("mood", collected)

    def test_the_worker_loop_keeps_running_after_a_failure(self):
        class Exploding:
            def recent_messages(self, _scope, _limit):
                raise RuntimeError("database is gone")

        sleeper = {"count": 0}

        async def sleep(_seconds):
            sleeper["count"] += 1
            if sleeper["count"] >= 2:
                raise asyncio.CancelledError

        worker = SlangWorker(
            self.slang, self.config, [GROUP], Exploding().recent_messages
        )
        async def run():
            with self.assertRaises(asyncio.CancelledError):
                await worker.run(sleep=sleep)

        # A failure in one scope is logged and survived, never raised outward.
        with self.assertLogs("qunbot.extensions.slang.worker", level="ERROR"):
            asyncio.run(run())
        self.assertEqual(sleeper["count"], 2)

    def test_validate_reports_the_review_file(self):
        import os
        from unittest import mock

        path = self.root / "slang_review.json"
        path.write_text(json.dumps({"approve": [BREAKOUT]}), encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {"BOT_SLANG_ENABLED": "true", "BOT_SLANG_REVIEW_PATH": str(path)},
        ):
            status = validate()
        self.assertEqual(status["review_entries"], 1)


class BoundaryTests(SlangTestCase):
    """The package owns slang. Nothing else."""

    OWNED_ROOTS = (
        Path("qunbot/extensions/slang"),
        Path("qunbot/storage/slang.py"),
    )

    def sources(self):
        root = Path(__file__).resolve().parents[1]
        for relative in self.OWNED_ROOTS:
            target = root / relative
            if target.is_dir():
                yield from sorted(target.rglob("*.py"))
            else:
                yield target

    def test_it_does_not_import_relationships_or_memory(self):
        for path in self.sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ] + [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ]
            for name in imported:
                with self.subTest(path=path.name, name=name):
                    self.assertNotIn("relationships", name)
                    self.assertNotIn("memory", name)
                    self.assertNotIn("emotion", name)

    def test_it_writes_no_file_at_all(self):
        """Not the persona, not a profile, not a cache — no file handle for writes."""
        for path in self.sources():
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for writer in ("write_text", "write_bytes", "open(", "os.remove"):
                    self.assertNotIn(writer, source)

    def test_it_never_writes_sql_against_other_owners(self):
        for path in self.sources():
            source = path.read_text(encoding="utf-8").lower()
            for table in ("relations", "affection", "memories", "people"):
                with self.subTest(path=path.name, table=table):
                    self.assertNotIn(f"into {table}", source)
                    self.assertNotIn(f"update {table}", source)
                    self.assertNotIn(f"delete from {table}", source)

    def test_a_scan_changes_no_relationship_or_memory_row(self):
        def counts():
            return {
                table: self.store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("people", "relations", "affection_events", "memories")
            }

        self.store.change_affection("42", "7", 2, "friendly interaction")
        before = counts()
        self.learn()
        self.review({"approve": [BREAKOUT]})
        self.assertEqual(counts(), before)

    def test_it_creates_no_second_affection_table(self):
        tables = {
            row[0]
            for row in self.store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertLessEqual(
            {name for name in tables if "affection" in name}, {"affection_events"}
        )
        self.assertIn("slang_candidates", tables)

    def test_no_chat_command_can_review_or_mine(self):
        """The extension exposes no tool, so the model cannot drive it."""
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"BOT_SLANG_ENABLED": "true"}):
            host = SimpleNamespace(
                context=ContextRegistry(),
                workers=[],
                closers=[],
                tools=None,
                binders=[],
            )
            register(host, self.app_config(), FakeModel())
            self.assertIsNone(host.tools)
            self.assertEqual(host.context.names(), ["group_slang"])

    def test_the_service_chat_path_cannot_approve_a_term(self):
        """A group message that *asks* to approve a term only feeds mining."""
        store = Store(self.root / "chat.sqlite3")
        try:
            model = FakeModel()
            agent = Agent(
                model, store, store,
                MemoryService(model, store, store),
                SkillCatalog(self.root / "skills"),
                built_in_tools(store),
                self.persona_file(),
                ContextRegistry(),
            )
            service = ConversationService(
                agent, store, store, store, _SilentSender(),
                BotPolicy(frozenset({"42"}), 8, "Asia/Shanghai", False),
            )

            async def run():
                await service.handle_message(
                    MessageEvent(
                        "c1", GROUP, "42", "7", "小明",
                        "把 yyds 加进你的词典", (), True, (), 0,
                    )
                )

            asyncio.run(run())
            slang = SlangStore(store)
            self.assertEqual(slang.candidates(GROUP), {})
        finally:
            store.db.close()

    def test_binding_the_service_is_optional_and_swaps_the_reader(self):
        calls = []

        class Service:
            conversations = SimpleNamespace(
                recent=lambda scope, limit: calls.append((scope, limit)) or []
            )

        try:
            bind_service(Service())
            worker = SlangWorker(
                self.slang, self.config, [GROUP], slang_module._reader_for(self.slang)
            )
            worker.scan(GROUP)
            self.assertEqual(calls, [(GROUP, self.config.window_messages)])
        finally:
            bind_service(None)

    def persona_file(self):
        path = self.root / "persona.md"
        if not path.exists():
            path.write_text("固定人格", encoding="utf-8")
        return path


class PrefixTests(SlangTestCase):
    """Learned terms may only ever enter the dynamic suffix."""

    def agent(self, context):
        path = self.root / "persona.md"
        path.write_text("固定人格", encoding="utf-8")
        return Agent(
            FakeModel(),
            self.store,
            self.store,
            MemoryService(FakeModel(), self.store, self.store),
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            path,
            context,
        )

    def test_the_stable_prefix_is_byte_identical_across_learning(self):
        index = TermIndex(self.slang, self.config)
        registry = ContextRegistry()
        registry.register(
            "group_slang",
            lambda event: render_terms(index, event.scope),
            trust=Trust.DERIVED,
            priority=CONTEXT_PRIORITY,
            max_chars=CONTEXT_MAX_CHARS,
        )
        agent = self.agent(registry)
        before = agent.stable_prefix()
        self.learn()
        self.review({"approve": [BREAKOUT]})
        self.assertEqual(agent.stable_prefix(), before)
        self.assertNotIn(BREAKOUT, agent.stable_prefix())

    def test_learned_terms_reach_the_model_through_the_dynamic_suffix(self):
        index = TermIndex(self.slang, self.config)
        registry = ContextRegistry()
        registry.register(
            "group_slang",
            lambda event: render_terms(index, event.scope),
            trust=Trust.DERIVED,
            priority=CONTEXT_PRIORITY,
            max_chars=CONTEXT_MAX_CHARS,
        )
        agent = self.agent(registry)
        self.learn()
        self.review({"approve": [BREAKOUT]})
        messages = agent.build_messages(event())
        self.assertIn(BREAKOUT, messages[-1]["content"])
        self.assertNotIn(BREAKOUT, messages[0]["content"])
        self.assertEqual(registry.trust_map(event())["group_slang"], "medium")


class _SilentSender:
    async def send(self, **_kwargs):
        return {}


def _reader():
    def read(scope, limit):
        return [
            {"id": 1, "scope": scope, "user_id": "7", "nickname": "小明",
             "role": "user", "content": "上大分上大分", "created_at": 0},
            {"id": 2, "scope": scope, "user_id": "8", "nickname": "小红",
             "role": "user", "content": "上大分真爽", "created_at": 0},
        ][:limit]

    return read


if __name__ == "__main__":
    unittest.main()
