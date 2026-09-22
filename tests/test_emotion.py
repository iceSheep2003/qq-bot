from __future__ import annotations

import asyncio
import json
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
)
from qunbot.emotion.evaluator import parse_verdict
from qunbot.extensions.proactive_chat.runner import ProactiveChat, ProactiveConfig
from qunbot.memory.service import MemoryService
from support import Store


class StubModel:
    """Answers with a canned string, ignoring the prompt."""

    def __init__(self, content: str = "收到。"):
        self.content = content

    async def complete(self, messages, tools=None, *, temperature=0.7):
        return {"choices": [{"message": {"content": self.content}}]}


class RecordingSender:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return {}


def event(message_id: int, *, at_bot: bool = True) -> MessageEvent:
    return MessageEvent(
        str(message_id), "group:42", "42", "7", "小明", "你今天真棒", (), at_bot, (), 0
    )


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


if __name__ == "__main__":
    unittest.main()
