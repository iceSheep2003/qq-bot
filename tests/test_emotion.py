from __future__ import annotations

import ast
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from qunbot.domain import MessageEvent
from qunbot.emotion import (
    DIMENSIONS,
    EmotionConfig,
    EmotionPolicy,
    Mood,
    build_emotion,
    loop_gain,
)
from qunbot.emotion.evaluator import dedupe_key, parse_verdict
from qunbot.emotion.store import EmotionStore
from qunbot.extensions.mood.register import register as register_mood
from qunbot.extensions.mood.register import validate as validate_mood
from qunbot.extensions.proactive_chat.runner import ProactiveChat, ProactiveConfig
from qunbot.memory.service import MemoryService
from qunbot.runtime.context import ContextRegistry, Trust
from support import Store


class StubModel:
    """Answers with a canned string, ignoring the prompt."""

    def __init__(self, content: str = "收到。"):
        self.content = content

    async def complete(self, messages, tools=None, *, temperature=0.7):
        return {"choices": [{"message": {"content": self.content}}]}


class CountingModel(StubModel):
    """Counts calls, so a test can prove a repeat never reached the model."""

    def __init__(self, content: str = "收到。"):
        super().__init__(content)
        self.calls = 0

    async def complete(self, messages, tools=None, *, temperature=0.7):
        self.calls += 1
        return await super().complete(messages, tools, temperature=temperature)


class RecordingSender:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return {}


class Clock:
    """A fixed clock the test advances by hand. Nothing here reads real time."""

    def __init__(self, now: int = 1_000_000):
        self.now = now

    def __call__(self) -> int:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += seconds


def event(message_id: int, *, at_bot: bool = True, text: str = "你今天真棒") -> MessageEvent:
    return MessageEvent(
        str(message_id), "group:42", "42", "7", "小明", text, (), at_bot, (), 0
    )


def imported_module_names(path: Path) -> list[str]:
    """Every module a source file names in an import, relative dots included."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append("." * node.level + (node.module or ""))
    return names


def table_snapshot(db) -> dict[str, list]:
    """Every table in a database file with its rows, for before/after compare."""
    tables = [
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    return {
        name: sorted((tuple(row) for row in db.execute(f"SELECT * FROM {name}")), key=repr)
        for name in sorted(tables)
    }


def verdict(deltas: dict, reason: str = "被群友夸了") -> str:
    return json.dumps({"deltas": deltas, "reason": reason})


def policy(**overrides) -> EmotionPolicy:
    base = {"half_life_minutes": 60, "sensitivity": 1.0, "min_sociability": 35}
    base.update(overrides)
    return EmotionPolicy(**base)


class EmotionTests(unittest.TestCase):
    """Decay, coupling, wording and verdict validation. No bot required."""

    def test_decay_toward_baseline_is_idempotent(self):
        policy_ = policy()
        happy = Mood(valence=100.0, updated_at=1_000_000)

        half_life_later = policy_.decay(happy, 1_000_000 + 3600)
        self.assertAlmostEqual(half_life_later.valence, 75.0, places=4)
        # Reading again at the same instant must not move the value: decay is
        # recomputed from the stored row, never written back.
        self.assertEqual(half_life_later, policy_.decay(happy, 1_000_000 + 3600))
        # A long sleep lands back on the baseline, with no background ticker.
        self.assertAlmostEqual(
            policy_.decay(happy, 1_000_000 + 86400 * 7).valence, 50.0, places=4
        )

    def test_coupling_drags_related_dimensions(self):
        stressed = policy().apply(
            Mood(updated_at=1_000_000), {"stress": 15}, "刚被骂了", 1_000_001
        )
        self.assertGreater(stressed.stress, 50)
        self.assertLess(stressed.energy, 50)
        self.assertLess(stressed.valence, 50)

    def test_deltas_stay_inside_the_range(self):
        policy_ = policy(sensitivity=3.0)
        self.assertLessEqual(
            policy_.apply(Mood(), {"valence": 15}, "被夸", 1).valence, 100.0
        )
        self.assertGreaterEqual(
            policy_.apply(Mood(), {"valence": -15}, "被骂", 1).valence, 0.0
        )

    def test_narration_contains_no_numbers(self):
        text = policy().narrate(Mood(valence=80, energy=20, stress=60))
        self.assertIn("心境", text)
        self.assertFalse(any(ch.isdigit() for ch in text), text)

    def test_narration_includes_the_stored_reason(self):
        self.assertIn("刚被群友夸了", policy().narrate(Mood(reason="刚被群友夸了")))

    def test_gate_closes_when_withdrawn(self):
        policy_ = policy(min_sociability=35)
        self.assertFalse(policy_.permits_proactive(Mood(sociability=10)))
        self.assertTrue(policy_.permits_proactive(Mood(sociability=90)))

    def test_parse_verdict_accepts_a_bounded_proposal(self):
        self.assertEqual(
            parse_verdict(verdict({"valence": 6, "energy": -3})),
            ({"valence": 6, "energy": -3}, "被群友夸了"),
        )

    def test_parse_verdict_rejects_bad_input(self):
        rejected = {
            "not json": "抱歉，我无法完成这个请求。",
            "oversized": verdict({"valence": 100}),
            "all zero": verdict({"valence": 0}),
            "empty reason": verdict({"valence": 6}, reason=""),
            "unknown keys": verdict({"libido": 6}),
            "too many": verdict({name: 1 for name in DIMENSIONS}),
            "deltas not a dict": json.dumps({"deltas": [1, 2], "reason": "被夸了"}),
            "non numeric": verdict({"valence": "很多"}),
        }
        for label, content in rejected.items():
            with self.subTest(label=label):
                self.assertIsNone(parse_verdict(content))


class EmotionSystemTests(unittest.TestCase):
    """The system as the bot actually uses it, backed by a real sqlite file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, **overrides) -> EmotionConfig:
        return replace(
            EmotionConfig(
                enabled=True,
                auto_enabled=True,
                db_path=self.root / "emotion.sqlite3",
                decay_minutes=60,
                sensitivity=1.0,
                min_sociability=35,
            ),
            **overrides,
        )

    def test_observe_applies_deltas_and_audits(self):
        system = build_emotion(self.config(), StubModel(verdict({"valence": 8})))
        try:
            asyncio.run(system.observe(event(1), "谢谢！"))
            mood = system.current("group:42")
            self.assertGreater(mood.valence, 50)
            self.assertEqual(mood.reason, "被群友夸了")
            self.assertEqual(system.store.event_count("group:42"), 1)
        finally:
            system.close()

    def test_observe_drops_malformed_output(self):
        for content in ("抱歉，我无法完成。", "{}", verdict({"valence": 100})):
            with self.subTest(content=content):
                system = build_emotion(self.config(), StubModel(content))
                try:
                    asyncio.run(system.observe(event(1), "收到。"))
                    self.assertEqual(system.store.load("group:42"), {})
                finally:
                    system.close()

    def test_auto_disabled_costs_nothing(self):
        system = build_emotion(self.config(auto_enabled=False), StubModel(""))
        try:
            asyncio.run(system.observe(event(1), "收到。"))
            self.assertEqual(system.store.load("group:42"), {})
        finally:
            system.close()

    def test_private_chat_is_not_assessed(self):
        system = build_emotion(self.config(), StubModel(verdict({"valence": 8})))
        try:
            asyncio.run(system.observe(event(1), "收到。"))
            private = MessageEvent(
                "p1", "private:7", None, "7", "小明", "你好", (), False, (), 0
            )
            asyncio.run(system.observe(private, "收到。"))
            self.assertEqual(system.store.load("private:7"), {})
        finally:
            system.close()

    def test_a_withdrawn_mood_closes_the_gate(self):
        system = build_emotion(self.config(), StubModel())
        try:
            self.assertTrue(system.permits_proactive("group:42"))
            system.store.change(
                "group:42", Mood(sociability=10).values(), {"sociability": -15}, "想安静"
            )
            self.assertFalse(system.permits_proactive("group:42"))
        finally:
            system.close()


class ProactiveGateTests(unittest.TestCase):
    """The gate is a decision, not a dice roll, so it is checked before the roll."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")
        self.persona = self.root / "persona.md"
        self.persona.write_text("固定人格", encoding="utf-8")
        for i in range(4):
            self.store.add_message(f"m{i}", "group:42", "7", "小明", "user", "在吗")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def service(self, emotions, sender):
        from qunbot.runtime.agent import Agent
        from qunbot.runtime.service import BotPolicy, ConversationService
        from qunbot.runtime.skills import SkillCatalog
        from qunbot.runtime.tools import built_in_tools

        agent = Agent(
            StubModel(),
            self.store,
            self.store,
            MemoryService(StubModel(), self.store, self.store),
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        policy_ = BotPolicy(
            frozenset({"42"}),
            8,
            "Asia/Shanghai",
            False,
            False,
            6,
            0,
            24,
            30,
            180,
            150,
        )
        return ConversationService(
            agent, self.store, self.store, self.store, sender, policy_,
        None, None, (emotions,),
        )

    def config(self) -> EmotionConfig:
        return EmotionConfig(
            enabled=True,
            auto_enabled=True,
            db_path=self.root / "emotion.sqlite3",
            decay_minutes=60,
            sensitivity=1.0,
            min_sociability=35,
        )

    def run_proactive(self, withdraw: bool) -> list[dict]:
        sender = RecordingSender()
        emotions = build_emotion(self.config(), StubModel())
        try:
            if withdraw:
                emotions.store.change(
                    "group:42",
                    Mood(sociability=10).values(),
                    {"sociability": -15},
                    "想安静待着",
                )
            # random.random(0) clears the existing probabilistic gate, so a
            # suppressed post can only be the mood gate's doing.
            with mock.patch("random.random", return_value=0.0):
                asyncio.run(
                    ProactiveChat(
                        self.service(emotions, sender), ProactiveConfig(), emotions
                    ).maybe_post("42")
                )
            return sender.sent
        finally:
            emotions.close()

    def test_withdrawn_bot_stays_quiet(self):
        self.assertEqual(self.run_proactive(withdraw=True), [])

    def test_sociable_bot_joins_in(self):
        self.assertEqual(
            [m["text"] for m in self.run_proactive(withdraw=False)], ["收到。"]
        )


class PromptContextTests(unittest.TestCase):
    """The mood is dynamic context: it must never reach the cacheable prefix."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")
        self.persona = self.root / "persona.md"
        self.persona.write_text("固定人格", encoding="utf-8")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_mood_reaches_the_prompt_but_not_the_stable_prefix(self):
        from qunbot.runtime.agent import Agent
        from qunbot.runtime.context import ContextRegistry
        from qunbot.runtime.skills import SkillCatalog
        from qunbot.runtime.tools import built_in_tools

        config = EmotionConfig(
            enabled=True,
            auto_enabled=True,
            db_path=self.root / "emotion.sqlite3",
            decay_minutes=60,
            sensitivity=1.0,
            min_sociability=35,
        )
        emotions = build_emotion(config, StubModel())
        try:
            context = ContextRegistry()
            context.register("mood", lambda e: emotions.narration(e.scope))
            agent = Agent(
                StubModel(),
                self.store,
                self.store,
                MemoryService(StubModel(), self.store, self.store),
                SkillCatalog(self.root / "skills"),
                built_in_tools(self.store),
                self.persona,
                context,
            )
            before = agent.stable_prefix()
            self.assertIn("心境", agent.build_messages(event(1))[-1]["content"])

            emotions.store.change(
                "group:42", Mood(valence=90).values(), {"valence": 15}, "被夸了"
            )
            self.assertIn("心境", agent.build_messages(event(1))[-1]["content"])
            self.assertEqual(agent.stable_prefix(), before)
        finally:
            emotions.close()


class Harness(unittest.TestCase):
    """A fixed clock and a private database. No test here reads real time."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()

    def config(self, **overrides) -> EmotionConfig:
        return replace(
            EmotionConfig(
                enabled=True,
                auto_enabled=True,
                db_path=self.root / "emotion.sqlite3",
                decay_minutes=60,
                sensitivity=1.0,
                min_sociability=35,
            ),
            **overrides,
        )

    def system(self, model=None, **overrides):
        system = build_emotion(
            self.config(**overrides), model or StubModel(verdict({"valence": 8})),
            clock=self.clock,
        )
        self.addCleanup(system.close)
        return system

    def observe(self, system, message_id: int, *, text: str = "你今天真棒", reply: str = "谢谢！"):
        asyncio.run(system.observe(event(message_id, text=text), reply))


class IdempotencyTests(Harness):
    """A repeat delivery must not move the mood a second time.

    The conversation service already admits each turn once, keyed by event id.
    These tests go around it and call ``observe`` directly, because the two
    protections are different: the caller's queue is about *work scheduling*,
    and this layer's ledger is about *state*, which must survive a restart, a
    replay harness, or a caller that never had a queue.
    """

    def test_the_same_event_twice_changes_the_mood_once(self):
        system = self.system(StubModel(verdict({"valence": 8})))
        self.observe(system, 1)
        once = system.current("group:42")
        self.observe(system, 1)
        self.assertEqual(system.current("group:42"), once)
        self.assertEqual(system.store.event_count("group:42"), 1)

    def test_a_repeat_costs_no_model_call(self):
        model = CountingModel(verdict({"valence": 8}))
        system = self.system(model)
        for _ in range(3):
            self.observe(system, 1)
        self.assertEqual(model.calls, 1)
        self.assertEqual(system.store.event_count("group:42"), 1)

    def test_distinct_turns_still_apply(self):
        system = self.system(StubModel(verdict({"valence": 8})))
        self.observe(system, 1)
        after_one = system.current("group:42")
        self.clock.advance(60)
        self.observe(system, 2)
        self.assertEqual(system.store.event_count("group:42"), 2)
        self.assertGreater(system.current("group:42").valence, after_one.valence)

    def test_the_same_message_id_in_another_group_is_its_own_turn(self):
        system = self.system(StubModel(verdict({"valence": 8})))
        self.observe(system, 1)
        other = MessageEvent(
            "1", "group:99", "99", "8", "小红", "你好", (), True, (), 0
        )
        asyncio.run(system.observe(other, "你好"))
        self.assertEqual(system.store.event_count("group:42"), 1)
        self.assertEqual(system.store.event_count("group:99"), 1)
        self.assertFalse(system.store.load("group:99") == system.store.load("group:42"))

    def test_a_rejected_verdict_leaves_the_turn_unclaimed(self):
        # A malformed verdict is dropped, so nothing was applied. The key must
        # stay free or an honest retry would be silently swallowed forever.
        system = self.system(StubModel("抱歉，我无法完成这个请求。"))
        self.observe(system, 1)
        self.assertFalse(system.store.seen("group:42", "1"))
        self.assertEqual(system.store.event_count("group:42"), 0)

        system.evaluator.model = StubModel(verdict({"valence": 8}))
        self.observe(system, 1)
        self.assertEqual(system.store.event_count("group:42"), 1)

    def test_the_claim_is_written_with_the_change_not_before_it(self):
        # The store is the backstop for two observers racing on the same turn:
        # the claim and the write share a transaction, so the loser writes
        # nothing at all.
        store = EmotionStore(self.root / "emotion.sqlite3")
        self.addCleanup(store.close)
        values = Mood(valence=60.0).values()

        self.assertTrue(
            store.change(
                "group:42", values, {"valence": 10}, "被夸了", dedupe_key="1", now=10
            )
        )
        self.assertFalse(
            store.change(
                "group:42", values, {"valence": 10}, "被夸了", dedupe_key="1", now=11
            )
        )
        self.assertEqual(store.event_count("group:42"), 1)
        self.assertEqual(store.load("group:42")["updated_at"], 10)

    def test_a_redelivery_after_a_restart_is_still_ignored(self):
        first = self.system(StubModel(verdict({"valence": 8})))
        self.observe(first, 1)
        saved = first.current("group:42")
        first.close()

        second = self.system(StubModel(verdict({"valence": 8})))
        self.assertTrue(second.store.seen("group:42", "1"))
        self.observe(second, 1)
        self.assertEqual(second.store.event_count("group:42"), 1)
        self.assertEqual(second.current("group:42"), saved)

    def test_an_id_less_event_falls_back_to_its_own_content(self):
        # Synthetic events have no platform id. They must still dedupe a
        # verbatim redelivery without collapsing two different messages into
        # one — that would wedge the mood permanently.
        first = MessageEvent("", "group:42", "42", "7", "小明", "在吗", (), True, (), 0)
        same = MessageEvent("", "group:42", "42", "7", "小明", "在吗", (), True, (), 0)
        other = MessageEvent("", "group:42", "42", "7", "小明", "晚安", (), True, (), 0)
        self.assertEqual(dedupe_key(first), dedupe_key(same))
        self.assertNotEqual(dedupe_key(first), dedupe_key(other))

        system = self.system(StubModel(verdict({"valence": 8})))
        asyncio.run(system.observe(first, "在的"))
        asyncio.run(system.observe(first, "在的"))
        self.assertEqual(system.store.event_count("group:42"), 1)
        asyncio.run(system.observe(other, "晚安"))
        self.assertEqual(system.store.event_count("group:42"), 2)


class ReplayTests(Harness):
    """Rebuild "what was the mood then?" from the event log alone."""

    def observed(self, turns: int = 3, *, step: int = 600, reply: str = "谢谢！"):
        system = self.system(StubModel(verdict({"valence": 8, "sociability": 6})))
        for index in range(turns):
            self.observe(system, index + 1, reply=reply)
            self.clock.advance(step)
        return system

    def test_replay_reproduces_the_stored_mood_exactly(self):
        system = self.observed()
        stored = system.store.load("group:42")
        rebuilt = system.replay("group:42")
        for name in DIMENSIONS:
            with self.subTest(dimension=name):
                self.assertAlmostEqual(getattr(rebuilt, name), stored[name], places=9)
        self.assertEqual(rebuilt.reason, stored["reason"])
        self.assertEqual(rebuilt.updated_at, stored["updated_at"])

    def test_replay_decayed_to_now_equals_the_live_read(self):
        system = self.observed()
        self.assertEqual(system.replay("group:42", upto=self.clock.now),
                         system.current("group:42"))

    def test_timeline_rebuilds_every_past_mood_with_its_reason(self):
        system = self.observed()
        history = system.history("group:42")
        self.assertEqual(
            [snapshot.at for snapshot in history],
            [1_000_000, 1_000_600, 1_001_200],
        )
        self.assertEqual([snapshot.reason for snapshot in history], ["被群友夸了"] * 3)
        self.assertEqual(
            [snapshot.deltas for snapshot in history],
            [{"valence": 8, "sociability": 6}] * 3,
        )
        # Each praise lifted the mood further; the last snapshot is the stored
        # state, and the first one is what the very first reply produced.
        values = [snapshot.mood.valence for snapshot in history]
        self.assertEqual(values, sorted(values))
        self.assertAlmostEqual(values[-1], system.replay("group:42").valence, places=9)
        self.assertLess(values[0], values[-1])

    def test_mood_at_answers_what_the_mood_was_between_two_turns(self):
        system = self.observed()
        history = system.history("group:42")

        # Asked for the instant of a recorded change, replay agrees with it.
        self.assertAlmostEqual(
            system.mood_at("group:42", 1_000_600).valence,
            history[1].mood.valence,
            places=9,
        )
        # Asked for an instant between two changes, it decays from the earlier
        # one rather than answering with the latest state.
        between = system.mood_at("group:42", 1_000_900)
        self.assertGreater(between.valence, 50.0)
        self.assertLess(between.valence, history[1].mood.valence)
        # Events after the requested instant are ignored, not folded and then
        # decayed: this is the difference between mood_at() and current().
        self.assertLess(system.mood_at("group:42", 1_000_900).valence,
                        system.current("group:42").valence)

    def test_replay_of_an_untouched_scope_is_the_baseline(self):
        system = self.system()
        self.assertEqual(system.history("never:seen"), ())
        self.assertEqual(system.replay("never:seen").values(),
                         Mood().values())
        self.assertEqual(system.mood_at("never:seen", 1_000_000).values(),
                         Mood().values())

    def test_replay_is_deterministic(self):
        system = self.observed(turns=5)
        self.assertEqual(system.replay("group:42"), system.replay("group:42"))
        self.assertEqual(system.history("group:42"), system.history("group:42"))

    def test_one_scope_cannot_replay_another(self):
        system = self.observed(turns=2)
        elsewhere = MessageEvent(
            "9", "group:99", "99", "8", "小红", "你好", (), True, (), 0
        )
        asyncio.run(system.observe(elsewhere, "你好"))
        self.assertEqual(len(system.history("group:42")), 2)
        self.assertEqual(len(system.history("group:99")), 1)
        self.assertEqual(system.store.event_count("group:42"), 2)
        self.assertAlmostEqual(
            system.replay("group:99").valence,
            system.history("group:99")[0].mood.valence,
            places=9,
        )


class RestartTests(Harness):
    """The state lives in SQLite, so a restart is a reconnect, not a reset."""

    def test_state_survives_a_restart(self):
        first = self.system(StubModel(verdict({"valence": 12})))
        self.observe(first, 1)
        saved = first.current("group:42")
        self.assertGreater(saved.valence, 50.0)
        self.assertEqual(saved.reason, "被群友夸了")
        first.close()

        self.clock.advance(3600)
        second = self.system(StubModel(verdict({"valence": 12})))
        recovered = second.current("group:42")
        self.assertEqual(recovered.reason, "被群友夸了")
        # The mood kept relaxing while the process was down, because decay is
        # computed from the stored timestamp rather than ticked in the
        # background.
        self.assertLess(recovered.valence, saved.valence)
        self.assertGreater(recovered.valence, 50.0)
        expected = EmotionPolicy(
            half_life_minutes=60, sensitivity=1.0, min_sociability=35
        ).decay(saved, self.clock.now)
        self.assertAlmostEqual(recovered.valence, expected.valence, places=9)

    def test_the_log_survives_a_restart_so_replay_still_works(self):
        first = self.system(StubModel(verdict({"valence": 8})))
        self.observe(first, 1)
        self.clock.advance(600)
        self.observe(first, 2)
        stored = first.store.load("group:42")
        first.close()

        second = self.system(StubModel(verdict({"valence": 8})))
        self.assertEqual(len(second.history("group:42")), 2)
        rebuilt = second.replay("group:42")
        for name in DIMENSIONS:
            with self.subTest(dimension=name):
                self.assertAlmostEqual(getattr(rebuilt, name), stored[name], places=9)


class CalibrationTests(unittest.TestCase):
    """Long-run behaviour on a fixed clock: does the mood relax, or latch?

    ``loop_gain`` is the criterion. One event multiplies a deviation from
    baseline by ``(I + coupling*C)`` and the decay between events multiplies it
    by ``exp(-T/tau)``; if the product exceeds 1 the mood is an amplifier and
    any steady stream of events — *including one with no net direction* — walks
    it to a clamp boundary and leaves it there.
    """

    def policy(self, coupling: float = 1.0, half_life: int = 180) -> EmotionPolicy:
        return EmotionPolicy(
            half_life_minutes=half_life,
            sensitivity=1.0,
            min_sociability=35,
            coupling=coupling,
        )

    def stream(self, policy, deltas_for, turns: int, *, step: int = 300) -> Mood:
        mood, now = Mood(updated_at=0), 0
        for index in range(turns):
            now += step
            mood = policy.apply(mood, deltas_for(index), "有来有回", now)
        return mood

    def test_a_balanced_stream_hovers_near_baseline_once_the_gain_is_under_one(self):
        policy = self.policy(coupling=0.0)
        self.assertLess(loop_gain(policy, cadence_seconds=300), 1.0)
        mood = self.stream(policy, lambda i: {"valence": 6 if i % 2 else -6}, 2000)
        self.assertLess(abs(mood.valence - 50.0), 5.0)
        for name in ("energy", "stress", "interest", "sociability"):
            with self.subTest(dimension=name):
                self.assertAlmostEqual(getattr(mood, name), 50.0, places=6)

    def test_the_shipped_coupling_makes_a_balanced_stream_saturate(self):
        # Characterization of the shipped feel, not an aspiration: with
        # coupling=1.0 and a 180-minute half-life the gain is > 1, so a stream
        # that is *exactly* balanced still ends pinned to an extreme, and which
        # extreme is decided by whichever way the first event leaned.
        policy = self.policy()
        self.assertGreater(loop_gain(policy, cadence_seconds=300), 1.0)
        warm_first = lambda i: {"valence": 6 if i % 2 == 0 else -6}  # noqa: E731
        mood = self.stream(policy, warm_first, 2000)
        self.assertEqual(mood.valence, 100.0)
        self.assertEqual(mood.sociability, 100.0)
        self.assertEqual(mood.stress, 0.0)
        # The mirror image: start on the negative half-cycle and it latches low.
        cold_first = lambda i: {"valence": -6 if i % 2 == 0 else 6}  # noqa: E731
        mirrored = self.stream(policy, cold_first, 2000)
        self.assertEqual(mirrored.valence, 0.0)

    def test_a_latched_mood_cannot_be_talked_back_down(self):
        # The practical consequence, on the shipped settings: 40 warm turns
        # peg the mood at the ceiling, and 80 cold turns — twice as many, twice
        # as long — do not move it at all, because the coupling feeds the
        # elevated neighbours back into valence faster than the deltas drain it.
        policy = self.policy()
        mood, now = Mood(updated_at=0), 0
        for _ in range(40):
            now += 300
            mood = policy.apply(mood, {"valence": 6}, "被夸", now)
        self.assertEqual(mood.valence, 100.0)
        for _ in range(80):
            now += 300
            mood = policy.apply(mood, {"valence": -6}, "被冷落", now)
        self.assertEqual(mood.valence, 100.0)

        # Without the coupling the same sequence is reversible.
        plain = self.policy(coupling=0.0)
        mood, now = Mood(updated_at=0), 0
        for _ in range(40):
            now += 300
            mood = plain.apply(mood, {"valence": 6}, "被夸", now)
        for _ in range(80):
            now += 300
            mood = plain.apply(mood, {"valence": -6}, "被冷落", now)
        self.assertLess(mood.valence, 10.0)

    def test_quiet_decay_brings_any_mood_back_to_baseline(self):
        # Whatever the coupling does under load, silence is always enough. This
        # is the safety net that keeps a latched mood from being permanent.
        for coupling in (0.0, 1.0):
            with self.subTest(coupling=coupling):
                policy = self.policy(coupling=coupling)
                mood = self.stream(policy, lambda i: {"stress": 12}, 200)
                self.assertGreater(mood.stress, 90.0)
                recovered = policy.decay(mood, 200 * 300 + 10 * 180 * 60)
                for name in DIMENSIONS:
                    with self.subTest(dimension=name):
                        self.assertLess(abs(getattr(recovered, name) - 50.0), 0.5)

    def test_no_dimension_ever_leaves_its_range(self):
        # A deterministic mixed stream, long enough to saturate: the clamps are
        # the only reason an unstable loop stays presentable, so pin them.
        script = (
            {"valence": 15},
            {"stress": 15},
            {"sociability": -15},
            {"energy": 12, "interest": 9},
            {"valence": -15, "stress": -8},
            {"interest": 15},
        )
        policy = self.policy()
        mood = self.stream(policy, lambda i: script[i % len(script)], 5000)
        for name in DIMENSIONS:
            with self.subTest(dimension=name):
                self.assertGreaterEqual(getattr(mood, name), 0.0)
                self.assertLessEqual(getattr(mood, name), 100.0)


class BoundaryTests(Harness):
    """Mood owns the bot's state and nothing else. Enforced, not intended."""

    def test_the_package_imports_nothing_that_owns_people_or_persona(self):
        root = Path(__file__).resolve().parents[1] / "qunbot" / "emotion"
        forbidden = ("relationships", "persona", "storage", "extensions", "affection")
        sources = sorted(root.glob("*.py"))
        self.assertTrue(sources)
        for path in sources:
            for name in imported_module_names(path):
                for word in forbidden:
                    with self.subTest(source=path.name, imported=name):
                        self.assertNotIn(word, name)

    def test_observing_leaves_the_application_database_untouched(self):
        store = Store(self.root / "bot.sqlite3")
        self.addCleanup(store.db.close)
        store.people.observe_user("7", "小明")
        store.people.change_affection("42", "7", 3, "聊得来")
        store.add_message("m1", "group:42", "7", "小明", "user", "在吗")
        before = table_snapshot(store.db)

        system = self.system(StubModel(verdict({"valence": 8, "stress": -6})))
        self.observe(system, 1)
        self.clock.advance(60)
        self.observe(system, 2)

        after = table_snapshot(store.db)
        self.assertEqual(before, after)
        # The mood tables are not merely unchanged — they are not here at all.
        self.assertNotIn("mood", after)

    def test_observing_never_rewrites_the_persona_file(self):
        persona = self.root / "persona.md"
        persona.write_text("固定人格：说话简短。\n", encoding="utf-8")
        before = persona.read_bytes()

        system = self.system(StubModel(verdict({"valence": 8})))
        self.observe(system, 1)
        self.observe(system, 1, text="你还记得我吗")

        self.assertEqual(persona.read_bytes(), before)

    def test_pointing_the_mood_at_the_shared_database_adds_only_its_own_tables(self):
        # BOT_MOOD_DB_PATH may name the main database. Even then the mood
        # creates its three tables and touches nothing else.
        shared = self.root / "bot.sqlite3"
        store = Store(shared)
        self.addCleanup(store.db.close)
        store.people.observe_user("7", "小明")
        store.people.change_affection("42", "7", 3, "聊得来")
        store.add_message("m1", "group:42", "7", "小明", "user", "在吗")
        before = table_snapshot(store.db)

        system = self.system(StubModel(verdict({"valence": 8})), db_path=shared)
        self.observe(system, 1)

        after = table_snapshot(store.db)
        self.assertEqual(
            set(after) - set(before), {"mood", "mood_events", "mood_observed"}
        )
        for name, rows in before.items():
            with self.subTest(table=name):
                self.assertEqual(after[name], rows)


class NoOpHost:
    """The slice of FeatureHost that mood.register touches."""

    def __init__(self):
        self.context = ContextRegistry()
        self.observers: list = []
        self.closers: list = []
        self.proactive_gate = None


class DisabledExtensionTests(Harness):
    """BOT_MOOD_ENABLED=false must cost exactly nothing."""

    def test_a_disabled_mood_creates_no_database_and_registers_nothing(self):
        target = self.root / "nested" / "emotion.sqlite3"
        with mock.patch.dict(
            os.environ,
            {"BOT_MOOD_ENABLED": "false", "BOT_MOOD_DB_PATH": str(target)},
        ):
            host = NoOpHost()
            register_mood(host, None, StubModel(verdict({"valence": 8})))

        self.assertFalse(target.exists())
        self.assertFalse(target.parent.exists())
        self.assertEqual(host.context.names(), [])
        self.assertEqual(host.observers, [])
        self.assertEqual(host.closers, [])
        self.assertIsNone(host.proactive_gate)

    def test_a_disabled_mood_reports_itself_in_the_startup_check(self):
        target = self.root / "emotion.sqlite3"
        with mock.patch.dict(
            os.environ,
            {"BOT_MOOD_ENABLED": "false", "BOT_MOOD_DB_PATH": str(target)},
        ):
            self.assertEqual(
                validate_mood(),
                {"enabled": False, "auto": True, "db_path": str(target)},
            )

    def test_an_enabled_mood_registers_its_contribution_at_priority_60(self):
        target = self.root / "emotion.sqlite3"
        with mock.patch.dict(
            os.environ,
            {
                "BOT_MOOD_ENABLED": "true",
                "BOT_MOOD_AUTO_ENABLED": "true",
                "BOT_MOOD_DB_PATH": str(target),
            },
        ):
            host = NoOpHost()
            register_mood(host, None, StubModel(verdict({"valence": 8})))
            self.assertTrue(target.exists())

            contribution = next(
                item for item in host.context.contributions(event(1))
                if item.name == "mood"
            )
            # persona sits at 65, so a tight budget drops the phrasing and
            # keeps the state. Changing 60 breaks that ordering.
            self.assertEqual(contribution.priority, 60)
            self.assertEqual(contribution.trust, Trust.DERIVED)
            self.assertEqual(contribution.max_chars, 200)

            self.assertEqual(len(host.observers), 1)
            self.assertIs(host.proactive_gate, host.observers[0])
            self.assertEqual(len(host.closers), 1)
            for closer in host.closers:
                closer()

    def test_the_enabled_mood_gate_only_closes_on_its_own_state(self):
        # The proactive gate is a mood decision, so a *disabled* module has no
        # gate at all: proactive_chat keeps its own dice and nothing else.
        host = NoOpHost()
        with mock.patch.dict(os.environ, {"BOT_MOOD_ENABLED": "false"}):
            register_mood(host, None, StubModel())
        self.assertIsNone(host.proactive_gate)


if __name__ == "__main__":
    unittest.main()
