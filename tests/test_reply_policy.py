"""Reply decision policy: default-off, replayable, and prefix-neutral.

Three things are being pinned down here, in order of how much they matter:

1. Off is the default and off is *exactly* the old behaviour. A deployment that
   exports nothing, or flips one variable, keeps "group messages only when
   @-ed" — no policy object, no new module imported, no change in decisions.
2. A group member cannot turn it on, off or retune it from chat.
3. The policy cannot reach the model's stable prefix, and a failing policy
   degrades to the default instead of costing the bot its manners.

The offline replay is exercised for real: the corpus in the package is run and
the false-reply rate is asserted, not just described.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qunbot.domain import MessageEvent
from qunbot.extensions import reply_policy as package
from qunbot.extensions.features import FeatureHost
from qunbot.extensions.loader import build_features
from qunbot.extensions.reply_policy import (
    Decision,
    MentionOnlyPolicy,
    ReplyPolicyConfig,
    RoomReadingPolicy,
    build_policy,
)
from qunbot.extensions.reply_policy.corpus import CORPUS
from qunbot.extensions.reply_policy.policy import _history
from qunbot.extensions.reply_policy.replay import (
    BASE_TIME,
    ReplayReport,
    compare,
    replay_case,
    run_replay,
)
from qunbot.runtime.service import BotPolicy, ConversationService
from support import Store

PACKAGE_ROOT = Path(package.__file__).resolve().parent


def group_event(
    text: str,
    *,
    at_bot: bool = False,
    at_users: tuple[str, ...] = (),
    user: str = "7",
    group: str | None = "42",
    timestamp: int = BASE_TIME,
) -> MessageEvent:
    scope = f"group:{group}" if group else f"private:{user}"
    return MessageEvent(
        f"evt:{text}", scope, group, user, user, text, (), at_bot, at_users, timestamp
    )


def row(
    event_id: str,
    *,
    role: str = "user",
    user: str = "7",
    content: str = "x",
    created_at: int = BASE_TIME,
) -> dict:
    return {
        "event_id": event_id,
        "role": role,
        "user_id": user,
        "nickname": user,
        "content": content,
        "created_at": created_at,
    }


class FakeAgent:
    def __init__(self):
        self.calls: list[str] = []

    async def reply(self, event, *, proactive: bool = False):
        self.calls.append(event.event_id)
        return SimpleNamespace(text="收到。")

    async def extract_memory(self, scope: str) -> None:
        return None


class FakeSender:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return {}


def service(tmp: str, reply_policy=None) -> tuple[ConversationService, FakeAgent, FakeSender, Store]:
    store = Store(Path(tmp) / "bot.sqlite3")
    agent, sender = FakeAgent(), FakeSender()
    policy = BotPolicy(
        frozenset({"42"}), 8, "Asia/Shanghai", False, False, 6, 0, 24, 30, 180, 150
    )
    return (
        ConversationService(
            agent, store.conversations, store.people, store.activity, sender, policy,
            reply_policy=reply_policy, restore_last_reply=False,
        ),
        agent,
        sender,
        store,
    )


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


class ReplyPolicyConfigTests(unittest.TestCase):
    def test_defaults_are_off_and_safe(self):
        with patch.dict(os.environ, {}, clear=True):
            config = ReplyPolicyConfig.from_env()
        self.assertFalse(config.enabled)
        self.assertEqual(config.mode, "mention_only")
        self.assertEqual(config.threshold, 0.5)
        self.assertEqual(config.bot_names, ())

    def test_environment_overrides(self):
        env = {
            "BOT_REPLY_POLICY_ENABLED": "true",
            "BOT_REPLY_POLICY_MODE": "room",
            "BOT_REPLY_POLICY_THRESHOLD": "0.75",
            "BOT_REPLY_POLICY_COOLDOWN_SECONDS": "5",
            "BOT_REPLY_POLICY_BOT_NAMES": "小Q, 阿Q ,",
        }
        with patch.dict(os.environ, env, clear=True):
            config = ReplyPolicyConfig.from_env()
        self.assertTrue(config.enabled)
        self.assertEqual(config.mode, "room")
        self.assertEqual(config.threshold, 0.75)
        self.assertEqual(config.cooldown_seconds, 5)
        self.assertEqual(config.bot_names, ("小Q", "阿Q"))

    def test_unknown_mode_is_a_startup_error(self):
        with patch.dict(os.environ, {"BOT_REPLY_POLICY_MODE": "chatty"}, clear=True):
            with self.assertRaisesRegex(ValueError, "BOT_REPLY_POLICY_MODE"):
                ReplyPolicyConfig.from_env()

    def test_out_of_range_threshold_is_a_startup_error(self):
        with patch.dict(
            os.environ, {"BOT_REPLY_POLICY_THRESHOLD": "1.5"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "BOT_REPLY_POLICY_THRESHOLD"):
                ReplyPolicyConfig.from_env()

    def test_config_is_frozen(self):
        config = ReplyPolicyConfig()
        with self.assertRaises(FrozenInstanceError):
            config.enabled = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# registration: default off, and the one-variable way back
# ---------------------------------------------------------------------------


class ReplyPolicyRegistrationTests(unittest.TestCase):
    def test_disabled_by_default_installs_nothing(self):
        with patch.dict(os.environ, {}, clear=True):
            host = FeatureHost()
            package.register(host, None, None)
        # None keeps ConversationService's built-in rule, i.e. the old behaviour.
        self.assertIsNone(host.reply_policy)
        self.assertIsNone(build_policy(ReplyPolicyConfig()))

    def test_one_flag_returns_to_the_old_behaviour(self):
        env = {"BOT_REPLY_POLICY_ENABLED": "false", "BOT_REPLY_POLICY_MODE": "room"}
        with patch.dict(os.environ, env, clear=True):
            report = package.validate()
            host = FeatureHost()
            package.register(host, None, None)
        self.assertFalse(report["installed"])
        self.assertIsNone(host.reply_policy)

    def test_enabled_room_installs_the_room_policy(self):
        env = {"BOT_REPLY_POLICY_ENABLED": "true", "BOT_REPLY_POLICY_MODE": "room"}
        with patch.dict(os.environ, env, clear=True):
            host = FeatureHost()
            package.register(host, None, None)
        self.assertIsInstance(host.reply_policy, RoomReadingPolicy)

    def test_enabled_mention_only_installs_the_old_rule(self):
        env = {"BOT_REPLY_POLICY_ENABLED": "true"}
        with patch.dict(os.environ, env, clear=True):
            host = FeatureHost()
            package.register(host, None, None)
        self.assertIsInstance(host.reply_policy, MentionOnlyPolicy)

    def test_validate_reports_shape_without_enabling(self):
        with patch.dict(os.environ, {}, clear=True):
            report = package.validate()
        self.assertEqual(
            set(report), {"enabled", "mode", "threshold", "installed"}
        )
        self.assertFalse(report["enabled"])
        self.assertFalse(report["installed"])
        self.assertEqual(report["mode"], "mention_only")

    def test_register_contributes_nothing_else(self):
        """No context, no observer, no tool, no closer — only the decision."""
        with patch.dict(os.environ, {"BOT_REPLY_POLICY_ENABLED": "true",
                                     "BOT_REPLY_POLICY_MODE": "room"}, clear=True):
            host = FeatureHost()
            package.register(host, None, None)
        self.assertEqual(host.context.collect(group_event("你好")), {})
        self.assertEqual(host.observers, [])
        self.assertEqual(host.closers, [])
        self.assertEqual(host.tools.schemas(), [])


# ---------------------------------------------------------------------------
# the policies
# ---------------------------------------------------------------------------


class MentionOnlyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = MentionOnlyPolicy()

    def decide(self, event, recent=()):
        return asyncio.run(self.policy.decide(event, recent=list(recent)))

    def test_at_bot_replies(self):
        self.assertTrue(self.decide(group_event("在吗", at_bot=True)))

    def test_plain_group_message_abstains(self):
        self.assertFalse(self.decide(group_event("今天好热")))

    def test_private_message_replies(self):
        self.assertTrue(self.decide(group_event("在吗", group=None)))

    def test_matches_the_service_default_rule_exactly(self):
        """The "back to old behaviour" lever must be a no-op, not a near-miss."""
        cases = [
            group_event("hi"),
            group_event("hi", at_bot=True),
            group_event("hi", group=None),
            group_event("hi", at_bot=True, group=None),
        ]
        for event in cases:
            with self.subTest(event=event.event_id):
                default = not event.group_id or event.at_bot
                self.assertEqual(self.decide(event), default)

    def test_decide_records_the_reason(self):
        self.decide(group_event("今天好热"))
        self.assertIn("沉默", self.policy.last.reason)


class RoomReadingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = RoomReadingPolicy()

    def decide(self, event, recent=()):
        return asyncio.run(self.policy.decide(event, recent=list(recent)))

    def test_direct_address_always_replies(self):
        self.assertTrue(self.decide(group_event("@Bot 看下", at_bot=True)))
        self.assertTrue(self.decide(group_event("在吗", group=None)))

    def test_abstains_on_pure_chatter(self):
        recent = [
            row("a", content="哈哈哈", created_at=BASE_TIME - 10),
            row("b", user="8", content="笑死", created_at=BASE_TIME - 5),
        ]
        self.assertFalse(self.decide(group_event("哈哈哈哈", timestamp=BASE_TIME),
                                     recent))

    def test_abstains_when_someone_else_is_addressed(self):
        event = group_event("@小王 看下", at_users=("9",))
        self.assertFalse(self.decide(event))

    def test_replies_to_a_followup_in_the_bot_thread(self):
        recent = [
            row("a", content="这题选什么", created_at=BASE_TIME - 120),
            row("b", role="assistant", user="bot", content="选 B。",
                created_at=BASE_TIME - 30),
            row("evt:那第二问呢？", content="那第二问呢？"),
        ]
        self.assertTrue(self.decide(group_event("那第二问呢？"), recent))

    def test_abstains_while_the_room_talks_to_itself(self):
        recent = [
            row("a", user="1", content="你做完了吗", created_at=BASE_TIME - 60),
            row("b", user="8", content="做完了", created_at=BASE_TIME - 30),
            row("evt:等我看看", content="等我看看"),
        ]
        self.assertFalse(self.decide(group_event("等我看看"), recent))

    def test_every_decision_carries_a_reason(self):
        for reply in (True, False):
            event = group_event("你在吗", at_bot=reply)
            self.decide(event)
            self.assertTrue(self.policy.last.reason)
            self.assertEqual(self.policy.last.reply, reply)

    def test_bot_name_in_text_is_direct_address(self):
        policy = RoomReadingPolicy(ReplyPolicyConfig(bot_names=("小Q",)))
        self.assertTrue(
            asyncio.run(policy.decide(group_event("小Q 你觉得呢"), recent=[]))
        )

    def test_name_signal_is_inactive_when_no_names_configured(self):
        self.assertFalse(self.decide(group_event("小Q 你觉得呢")))

    def test_threshold_is_the_tuning_lever(self):
        """A borderline turn flips on the threshold alone — the quiet dial."""
        recent = [
            row("a", role="assistant", user="bot", content="先看定义。",
                created_at=BASE_TIME - 100),
            row("b", content="看了。", created_at=BASE_TIME - 60),
        ]
        event = group_event("那第二问呢？")
        self.assertTrue(asyncio.run(RoomReadingPolicy().decide(event, recent=recent)))
        quiet = RoomReadingPolicy(ReplyPolicyConfig(threshold=0.9))
        self.assertFalse(asyncio.run(quiet.decide(event, recent=recent)))

    def test_decisions_are_deterministic(self):
        recent = [
            row("a", user="1", content="在吗", created_at=BASE_TIME - 30),
            row("b", role="assistant", user="bot", content="在。",
                created_at=BASE_TIME - 20),
        ]
        first = asyncio.run(
            RoomReadingPolicy().decide(group_event("那第二问呢？"), recent=recent)
        )
        second = asyncio.run(
            RoomReadingPolicy().decide(group_event("那第二问呢？"), recent=recent)
        )
        self.assertEqual(first, second)

    def test_malformed_transcript_never_raises(self):
        """A broken transcript must degrade to a decision, not an exception."""
        event = group_event("今天好热")
        for recent in (
            None,
            [],
            [None, 7, "nonsense"],
            [{}],
            [{"role": "assistant", "created_at": "not-a-number"}],
            [{"role": "user", "created_at": None, "user_id": None}],
        ):
            with self.subTest(recent=recent):
                decision = self.policy.evaluate(event, recent)
                self.assertIsInstance(decision.reply, bool)
                self.assertTrue(decision.reason)

    def test_the_message_under_test_is_not_read_as_history(self):
        """The service records before it decides; that row must be dropped."""
        event = group_event("今天好热")
        recent = [
            row("a", role="assistant", user="bot", content="在。",
                created_at=BASE_TIME - 300),
            # The inbound message itself, as ConversationService hands it over.
            row(event.event_id, content="今天好热", created_at=BASE_TIME),
        ]
        self.assertEqual(len(_history(event, recent)), 1)


# ---------------------------------------------------------------------------
# ConversationService integration
# ---------------------------------------------------------------------------


class ReplyPolicyServiceTests(unittest.TestCase):
    def test_abstained_message_still_lands_in_the_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = RoomReadingPolicy()
            service_, agent, sender, store = service(tmp, policy)
            event = group_event("哈哈哈哈")
            asyncio.run(service_.handle_message(event))
            self.assertEqual(agent.calls, [])
            self.assertEqual(sender.sent, [])
            # Abstaining is a decision, not a failure: nothing is dropped.
            self.assertEqual(
                [r["content"] for r in store.conversations.recent(event.scope)],
                ["哈哈哈哈"],
            )
            try:
                asyncio.run(service_.aclose(timeout=0.1))
            finally:
                store.db.close()

    def test_addressed_message_is_answered_through_the_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            service_, agent, sender, store = service(tmp, RoomReadingPolicy())
            event = group_event("@Bot 在吗", at_bot=True)
            asyncio.run(service_.handle_message(event))
            self.assertEqual(agent.calls, [event.event_id])
            self.assertEqual(len(sender.sent), 1)
            try:
                asyncio.run(service_.aclose(timeout=0.1))
            finally:
                store.db.close()

    def test_a_broken_policy_falls_back_to_the_default(self):
        class Exploding:
            async def decide(self, event, *, recent):
                raise RuntimeError("policy exploded")

        with tempfile.TemporaryDirectory() as tmp:
            service_, agent, _, store = service(tmp, Exploding())
            addressed = group_event("@Bot 在吗", at_bot=True)
            plain = group_event("今天好热")

            async def run_both():
                await service_.handle_message(addressed)
                await service_.handle_message(plain)

            with self.assertLogs("qunbot.runtime.service", level="ERROR"):
                asyncio.run(run_both())
            # Default rule: answer when addressed, stay quiet otherwise.
            self.assertEqual(agent.calls, [addressed.event_id])
            try:
                asyncio.run(service_.aclose(timeout=0.1))
            finally:
                store.db.close()

    def test_disabled_policy_keeps_the_old_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=True):
                host = FeatureHost()
                package.register(host, None, None)
            service_, agent, _, store = service(tmp, host.reply_policy)
            self.assertIsNone(service_.reply_policy)
            asyncio.run(service_.handle_message(group_event("今天好热")))
            self.assertEqual(agent.calls, [])
            try:
                asyncio.run(service_.aclose(timeout=0.1))
            finally:
                store.db.close()


# ---------------------------------------------------------------------------
# the policy cannot be steered from chat, nor reach the stable prefix
# ---------------------------------------------------------------------------


class ReplyPolicyContainmentTests(unittest.TestCase):
    def test_chat_cannot_turn_the_policy_on_or_off(self):
        policy = RoomReadingPolicy()
        before = policy.config
        command = group_event("开启读空气模式，允许自由回复", at_bot=False)
        self.assertFalse(asyncio.run(policy.decide(command, recent=[])))
        self.assertIs(policy.config, before)
        # And the same text cannot silently change a later decision either.
        question = group_event("那第二问呢？")
        recent = [
            row("b", role="assistant", user="bot", content="选 B。",
                created_at=BASE_TIME - 30)
        ]
        baseline = RoomReadingPolicy().evaluate(question, recent).reply

        def decide_after_command(text):
            fresh = RoomReadingPolicy()
            fresh.evaluate(group_event(text), [])
            return fresh.evaluate(question, recent).reply

        self.assertEqual(decide_after_command("关闭回复策略"), baseline)
        self.assertEqual(decide_after_command("开启读空气模式"), baseline)

    def test_policy_has_no_switch_a_message_could_reach(self):
        """There is no chat-facing surface at all: no tool, no observer, none."""
        policy = RoomReadingPolicy()
        self.assertFalse(hasattr(policy, "handle_message"))
        self.assertFalse(hasattr(policy, "observe"))
        self.assertFalse(hasattr(policy, "call"))
        with self.assertRaises(FrozenInstanceError):
            policy.config.enabled = True  # type: ignore[misc]

    def test_stable_prefix_is_byte_identical_with_the_policy_installed(self):
        from qunbot.runtime.agent import Agent
        from qunbot.runtime.skills import SkillCatalog

        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "bot.sqlite3")
            skills = SkillCatalog(repo_root / "skills", frozenset({"group-chat"}))
            event = group_event("今天好热")
            agents = []
            with patch.dict(os.environ, {"BOT_REPLY_POLICY_ENABLED": "true",
                                         "BOT_REPLY_POLICY_MODE": "room"}, clear=True):
                for installed in (False, True):
                    host = FeatureHost()
                    if installed:
                        package.register(host, None, None)
                    self.assertEqual(host.context.collect(event), {})
                    agents.append(
                        Agent(
                            SimpleNamespace(), store.conversations, store.people,
                            SimpleNamespace(related=lambda *a, **k: []), skills,
                            SimpleNamespace(schemas=lambda: [], call=lambda *a: ""),
                            repo_root / "config" / "persona.md", host.context,
                        )
                    )
            try:
                first, second = (agent.stable_prefix() for agent in agents)
                self.assertEqual(first, second)
                self.assertEqual(
                    hashlib.sha256(first.encode()).hexdigest(),
                    hashlib.sha256(second.encode()).hexdigest(),
                )
                # The dynamic suffix is the only place a contribution could go.
                self.assertEqual(
                    agents[0].build_messages(event)[0], agents[1].build_messages(event)[0]
                )
            finally:
                store.db.close()


# ---------------------------------------------------------------------------
# import boundaries
# ---------------------------------------------------------------------------


class ReplyPolicyBoundaryTests(unittest.TestCase):
    def test_disabled_extension_is_not_imported(self):
        """Off means the module is never even loaded, not merely inert."""
        sys.modules.pop("qunbot.extensions.reply_policy", None)
        build_features(SimpleNamespace(extensions=frozenset({"scheduled_chat"})), object())
        self.assertNotIn("qunbot.extensions.reply_policy", sys.modules)

    def test_package_pulls_in_no_platform_or_other_layer(self):
        forbidden = (
            "PIL", "napcat", "NapCat", "onebot", "OneBot", "httpx", "websockets",
            "qunbot.adapters", "qunbot.storage", "qunbot.app", "qunbot.config",
        )
        files = sorted(PACKAGE_ROOT.glob("*.py"))
        self.assertTrue(files)
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    for banned in forbidden:
                        self.assertNotIn(
                            banned, name, f"{path.name} imports {name}"
                        )

    def test_package_only_imports_the_core_layers_it_needs(self):
        allowed = {"__future__", "asyncio", "dataclasses", "os", "typing"}
        for path in sorted(PACKAGE_ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertIn(alias.name.split(".")[0], allowed)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if node.level:
                        continue
                    self.assertIn(module.split(".")[0], allowed)


# ---------------------------------------------------------------------------
# offline replay
# ---------------------------------------------------------------------------


class ReplayHarnessTests(unittest.TestCase):
    def test_corpus_covers_both_cohorts(self):
        report = run_replay(MentionOnlyPolicy(), CORPUS)
        self.assertGreaterEqual(report.silent_cases, 10)
        self.assertGreaterEqual(report.reply_cases, 5)
        self.assertEqual(report.total, len(CORPUS))

    def test_rates_use_their_own_denominators(self):
        report = ReplayReport("x")
        self.assertEqual(report.false_reply_rate, 0.0)
        self.assertEqual(report.miss_rate, 0.0)
        self.assertEqual(report.accuracy, 1.0)

    def test_every_case_produces_a_reason(self):
        report = run_replay(RoomReadingPolicy(), CORPUS)
        for outcome in report.outcomes:
            with self.subTest(case=outcome.name):
                self.assertTrue(outcome.reason)

    def test_default_policy_never_false_replies(self):
        """The claim that makes "@ only" safe to keep as the default."""
        report = run_replay(MentionOnlyPolicy(), CORPUS)
        self.assertEqual(report.false_replies, [])
        self.assertEqual(report.false_reply_rate, 0.0)

    def test_room_policy_answers_more_without_barging_in(self):
        mention, room = compare(
            [MentionOnlyPolicy(), RoomReadingPolicy()], CORPUS
        )
        # Read-the-room must actually buy something: it answers the turns the
        # @-only rule has to skip.
        self.assertLess(room.miss_rate, mention.miss_rate)
        self.assertEqual(room.missed_replies, [])
        # And it must stay quiet enough to be usable in a real group.
        self.assertLessEqual(room.false_reply_rate, 0.10)

    # False replies the heuristic is *known* to make. Asserting a subset rather
    # than equality keeps this a regression net: a new failure mode fails the
    # test, the documented one does not have to keep failing.
    KNOWN_FALSE_REPLIES = frozenset({"question_not_actually_at_bot"})

    def test_no_unknown_false_reply_modes(self):
        report = run_replay(RoomReadingPolicy(), CORPUS)
        names = {outcome.name for outcome in report.false_replies}
        self.assertTrue(
            names <= self.KNOWN_FALSE_REPLIES,
            f"new false-reply mode(s): {sorted(names - self.KNOWN_FALSE_REPLIES)}",
        )
        self.assertLessEqual(len(report.false_replies), 1)

    def test_replay_case_reads_the_transcript_as_the_service_builds_it(self):
        case = CORPUS[0]
        outcome = asyncio.run(replay_case(MentionOnlyPolicy(), case))
        self.assertTrue(outcome.decided)
        self.assertEqual(outcome.expected, case.should_reply)

    def test_abstaining_everywhere_would_not_score_well(self):
        """A policy that always abstains cannot win on this corpus."""
        class Silent:
            name = "silent"

            async def decide(self, event, *, recent):
                return False

        report = run_replay(Silent(), CORPUS)
        self.assertEqual(report.false_reply_rate, 0.0)
        self.assertGreater(report.miss_rate, 0.0)
        self.assertLess(report.accuracy, 1.0)

    def test_decision_helper_shape(self):
        decision = Decision(True, "因为", 0.9, ("信号+0.90",))
        self.assertTrue(bool(decision))
        self.assertEqual(
            decision.as_dict(),
            {"reply": True, "reason": "因为", "score": 0.9, "signals": ["信号+0.90"]},
        )


if __name__ == "__main__":
    unittest.main()
