"""The post-message interval continuation: AIReplay's "X minutes after".

Two things are being pinned here.

**The workflow.** A group falls quiet; only then does the bot consider a
continuation. The window is defined by the newest human message, its state
(answered? how many silent decisions so far?) survives a restart, and every
outcome — spoken or silent — is written down with a reason.

**The seams.** ``continuation`` is an ordinary scheduling action: it ships as a
disabled ``every`` job, and a quiet window produces a ``skipped`` run with the
reason in ``job_runs.detail``. The in-process ``proactive_chat`` worker drives
the same engine and the same persisted state, so running both cannot make the
bot say the same thing twice.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from qunbot.extensions.scheduled_chat.engine import (
    Decision,
    ProactiveChat,
    local_hour,
    mood_gate,
)
from qunbot.extensions.scheduled_chat.policy import (
    GroupPolicy,
    GroupPolicySet,
    ProactiveConfig,
    in_quiet_hours,
)
from qunbot.extensions.proactive_chat.runner import build_worker
from qunbot.extensions.scheduled_chat.job import (
    ChatJobHandler,
    ContinuationJobHandler,
    register_jobs,
)
from qunbot.scheduling import JobHandlerRegistry
from qunbot.scheduling.runner import JobRunner
from qunbot.scheduling.scheduler import Scheduler
from qunbot.storage.continuation import DECISION_HISTORY, ContinuationStore
from support import Store

GROUP = "42"
SCOPE = "group:42"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class RecordingSender:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return {}


class StubAgent:
    """Answers with a canned string and records the events it saw."""

    def __init__(self, text: str = "收到。"):
        self.text = text
        self.events = []

    async def reply(self, event, *, proactive: bool = False):
        self.events.append((event, proactive))
        return SimpleNamespace(text=self.text)

    async def extract_memory(self, scope: str) -> None:  # pragma: no cover
        return None


class StubMood:
    def __init__(self, permits: bool = True):
        self.permits = permits
        self.asked = []

    def permits_proactive(self, scope: str) -> bool:
        self.asked.append(scope)
        return self.permits

    async def observe(self, event, bot_reply):  # pragma: no cover
        return None


class FakeBot:
    """The surface both ``JobRuntime`` and the engine actually use.

    Backed by the real repositories, so persistence is real and only the
    transport is fake.
    """

    def __init__(
        self,
        store,
        *,
        allowed=("42",),
        agent=None,
        mood=None,
        hour=12,
        awake=True,
    ):
        self.store = store
        self.conversations = store.conversations
        self.activity = store.activity
        self.sender = RecordingSender()
        self.agent = agent if agent is not None else StubAgent()
        # The full BotPolicy surface both job strategies read.
        self.policy = SimpleNamespace(
            allowed_groups=frozenset(allowed),
            timezone="Asia/Shanghai",
            job_daily_limit=6,
            job_cooldown_minutes=30,
            job_freshness_minutes=180,
            job_max_chars=150,
        )
        self.last_reply = {}
        self.observers = (mood,) if mood is not None else ()
        self.hour = hour
        self.awake = awake
        self._locks = {}

    # --- JobRuntime surface ---
    def scope_lock(self, scope):
        return self._locks.setdefault(scope, asyncio.Lock())

    async def send_reply(self, group_id, user_id, text):
        await self.sender.send(group_id=group_id, user_id=user_id, text=text)
        return text

    def today_start(self):
        return 0

    def local_today(self):
        return date(2026, 1, 1)

    # --- ConversationService surface the engine optionally uses ---
    def local_hour(self):
        return self.hour

    def within_active_hours(self):
        return self.awake


def cfg(**overrides) -> ProactiveConfig:
    return ProactiveConfig(**overrides)


class ContinuationTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")
        self._seed_seq = 0

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    # --- helpers ---

    def seed(self, *, count: int = 4, age: int = 0, content: str = "在吗", scope=SCOPE):
        """Put ``count`` messages in the group, all ``age`` seconds old.

        Event ids are unique per call, so seeding twice models new messages
        arriving rather than a redelivered frame.
        """
        stamp = int(time.time()) - age
        for _ in range(count):
            self._seed_seq += 1
            self.store.conversations.add_message(
                f"{scope}:{self._seed_seq}", scope, "7", "小明", "user", content
            )
        self.store.db.execute(
            "UPDATE messages SET created_at=? WHERE scope=?", (stamp, scope)
        )

    def bot(self, **kwargs) -> FakeBot:
        return FakeBot(self.store, **kwargs)

    def engine(self, bot=None, config=None, mood=None, policies=None) -> ProactiveChat:
        bot = bot if bot is not None else self.bot()
        return ProactiveChat(
            bot, config if config is not None else cfg(), mood, policies
        )

    def decide(self, bot=None, config=None, mood=None, policies=None) -> Decision:
        engine = self.engine(bot, config, mood, policies)
        return asyncio.run(engine.maybe_post(GROUP))

    def row(self, sql, params=()):
        return self.store.db.execute(sql, params).fetchone()

    def state(self):
        return ContinuationStore(self.store).state(SCOPE)

    def clear(self):
        """Drop the persisted window, so one test can make two independent calls."""
        ContinuationStore(self.store).forget_scope(SCOPE)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class StorageTests(ContinuationTestCase):
    def test_migration_creates_both_tables(self):
        names = {
            row[0]
            for row in self.store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("continuation_state", names)
        self.assertIn("continuation_decisions", names)

    def test_reading_state_neither_writes_nor_crashes(self):
        store = ContinuationStore(self.store)
        self.assertEqual(
            store.state(SCOPE),
            {
                "window_closed_at": None,
                "last_attempt_at": 0,
                "silent_streak": 0,
                "last_outcome": "",
            },
        )
        self.assertEqual(self.row("SELECT count(*) FROM continuation_state")[0], 0)

    def test_close_window_resets_the_streak_and_audits(self):
        store = ContinuationStore(self.store)
        store.note_attempt(SCOPE, GROUP, 100, "the dice said stay quiet")
        store.close_window(SCOPE, GROUP, 200, "posted")
        state = store.state(SCOPE)
        self.assertEqual(state["window_closed_at"], 200)
        self.assertEqual(state["silent_streak"], 0)
        self.assertEqual(state["last_outcome"], "posted")
        self.assertEqual(
            [row["outcome"] for row in store.decisions(GROUP)],
            ["posted", "silent"],
        )

    def test_note_attempt_only_grows_the_streak(self):
        store = ContinuationStore(self.store)
        for index in range(3):
            store.note_attempt(SCOPE, GROUP, 100 + index, "silent")
        self.assertEqual(store.state(SCOPE)["silent_streak"], 3)

    def test_decisions_are_pruned_per_group(self):
        store = ContinuationStore(self.store)
        for index in range(DECISION_HISTORY + 25):
            store.note_observed(GROUP, SCOPE, index, f"reason {index}")
        kept = self.row(
            "SELECT count(*) FROM continuation_decisions WHERE group_id=?", (GROUP,)
        )[0]
        self.assertEqual(kept, DECISION_HISTORY)
        # The newest survive, the oldest are gone.
        self.assertEqual(store.decisions(GROUP, 1)[0]["reason"], "reason 224")

    def test_continuation_state_carries_no_user_id(self):
        """The privacy sweep deletes by user_id; this state is about the room."""
        columns = {
            row[1]
            for row in self.store.db.execute("PRAGMA table_info(continuation_state)")
        }
        self.assertNotIn("user_id", columns)

    def test_forget_scope_removes_both_tables(self):
        store = ContinuationStore(self.store)
        store.note_attempt(SCOPE, GROUP, 100, "silent")
        store.close_window(SCOPE, GROUP, 200, "posted")
        self.assertEqual(store.forget_scope(SCOPE), 3)
        self.assertEqual(store.state(SCOPE)["window_closed_at"], None)
        self.assertEqual(store.decisions(GROUP), [])

    def test_consecutive_identical_decisions_are_folded(self):
        """The table records what changed, not one row per tick."""
        store = ContinuationStore(self.store)
        store.note_observed(GROUP, SCOPE, 1, "group is still talking")
        store.note_observed(GROUP, SCOPE, 2, "group is still talking")
        store.note_observed(GROUP, SCOPE, 3, "waiting for the group to speak again")
        self.assertEqual(
            [row["reason"] for row in store.decisions(GROUP)],
            ["waiting for the group to speak again", "group is still talking"],
        )
        self.assertEqual(
            self.row("SELECT count(*) FROM continuation_decisions")[0], 2
        )


# --------------------------------------------------------------------------
# The quiet window
# --------------------------------------------------------------------------


class QuietWindowTests(ContinuationTestCase):
    def test_group_still_talking_is_not_a_continuation_opportunity(self):
        self.seed(age=60)
        decision = self.decide(config=cfg(quiet_minutes=10))
        self.assertFalse(decision.posted)
        self.assertEqual(decision.reason, "group is still talking")
        self.assertEqual(self.state()["last_attempt_at"], 0)

    def test_group_quiet_long_enough_gets_one_line(self):
        self.seed(age=900)
        bot = self.bot()
        decision = self.decide(bot=bot, config=cfg(quiet_minutes=10, probability=1.0))
        self.assertTrue(decision.posted, decision.reason)
        self.assertEqual([m["text"] for m in bot.sender.sent], ["收到。"])
        self.assertEqual(
            self.store.activity.proactive_count_since(GROUP, 0, "random"), 1
        )
        self.assertIsNotNone(self.state()["window_closed_at"])

    def test_the_window_stays_closed_until_the_group_speaks_again(self):
        quiet = cfg(quiet_minutes=10, probability=1.0)
        self.seed(age=900)
        self.assertTrue(self.decide(config=quiet).posted)
        # Same quiet stretch, second look: still nothing, and no second post.
        self.assertEqual(
            self.decide(config=quiet).reason, "waiting for the group to speak again"
        )
        # A message that arrives *after* the window closed reopens it. Time
        # has to move for that, so the close is aged rather than the messages:
        # the window was answered 30 minutes ago, the group spoke 15 minutes
        # ago, and has been quiet since. It is a fresh conversation.
        self.seed(count=1, age=900, content="有人吗")
        ContinuationStore(self.store).close_window(
            SCOPE, GROUP, int(time.time()) - 1800, "posted"
        )
        # A different line, since repeating the earlier one is also refused.
        bot = self.bot(agent=StubAgent("在的。"))
        self.assertTrue(self.decide(bot=bot, config=quiet).posted)
        self.assertEqual([m["text"] for m in bot.sender.sent], ["在的。"])

    def test_a_window_that_goes_stale_is_abandoned_once(self):
        self.seed(age=20_000)
        config = cfg(quiet_minutes=10, freshness_minutes=180, probability=1.0)
        self.assertEqual(self.decide(config=config).reason, "quiet window went stale")
        # Abandoning closes the window rather than re-deciding it every tick.
        self.assertEqual(
            self.decide(config=config).reason, "waiting for the group to speak again"
        )

    def test_too_little_history_is_not_a_conversation(self):
        self.seed(count=2, age=900)
        self.assertEqual(
            self.decide(config=cfg(quiet_minutes=10, min_messages=3)).reason,
            "group has not spoken enough",
        )

    def test_short_group_history_is_never_continued_in(self):
        self.seed(count=0, age=0)
        self.assertEqual(
            self.decide(config=cfg(quiet_minutes=10)).reason,
            "group has not spoken enough",
        )


# --------------------------------------------------------------------------
# Restart behaviour
# --------------------------------------------------------------------------


class RestartTests(ContinuationTestCase):
    def test_a_closed_window_survives_a_restart(self):
        config = cfg(quiet_minutes=10, probability=1.0)
        self.seed(age=900)
        self.assertTrue(self.decide(config=config).posted)
        # A brand new engine over the same database is what a restart looks
        # like: the in-memory dice are gone, the decision is not.
        self.assertEqual(
            self.decide(config=config).reason, "waiting for the group to speak again"
        )

    def test_an_attempt_backoff_survives_a_restart(self):
        self.seed(age=900)
        agent = StubAgent(text="")
        config = cfg(quiet_minutes=10, probability=1.0)
        first = self.decide(bot=self.bot(agent=agent), config=config)
        self.assertEqual(first.reason, "the model chose to stay silent")
        second = self.decide(config=config)
        self.assertEqual(second.reason, "already considered this quiet window")

    def test_the_backoff_lengthens_with_the_silent_streak(self):
        self.seed(age=900)
        config = cfg(quiet_minutes=10, retry_minutes=30, freshness_minutes=180)
        engine = self.engine(config=config)
        # The most recent message is older than the last attempt: same stretch.
        window = 500
        state = {"silent_streak": 0, "last_attempt_at": 600}
        self.assertEqual(engine._retry_seconds(config, state, window), 30 * 60)
        state["silent_streak"] = 3
        self.assertEqual(engine._retry_seconds(config, state, window), 120 * 60)
        state["silent_streak"] = 99
        # Capped by the freshness window, so a stale window is abandoned
        # rather than retried forever.
        self.assertEqual(engine._retry_seconds(config, state, window), 180 * 60)

    def test_a_silent_streak_does_not_follow_the_bot_into_a_new_conversation(self):
        """The streak belongs to one quiet stretch, not to the group forever."""
        config = cfg(quiet_minutes=10, retry_minutes=30, freshness_minutes=180)
        engine = self.engine(config=config)
        stale = {"silent_streak": 5, "last_attempt_at": 400}
        # The group has spoken since the last attempt: fresh conversation.
        self.assertEqual(engine._retry_seconds(config, stale, 900), 30 * 60)
        # Nobody has spoken since: the same stretch, so the wait stands.
        self.assertEqual(engine._retry_seconds(config, stale, 300), 180 * 60)

    def test_both_trigger_paths_share_one_window(self):
        """The worker path and the scheduler path cannot double-post."""
        self.seed(age=900)
        config = cfg(quiet_minutes=10, probability=1.0)
        worker_bot = self.bot()
        self.assertTrue(self.decide(bot=worker_bot, config=config).posted)
        schedule_bot = self.bot()
        self.assertFalse(self.decide(bot=schedule_bot, config=config).posted)
        self.assertEqual(schedule_bot.sender.sent, [])


# --------------------------------------------------------------------------
# The gates
# --------------------------------------------------------------------------


class GateTests(ContinuationTestCase):
    def setUp(self):
        super().setUp()
        self.seed(age=900)
        # Everything open unless a test closes it.
        self.open_config = cfg(
            quiet_minutes=10, probability=1.0, interval_minutes=0, daily_limit=2
        )

    def test_the_dice_can_say_stay_quiet(self):
        with mock.patch("random.random", return_value=1.0):
            decision = self.decide(config=cfg(quiet_minutes=10, probability=0.15))
        self.assertEqual(decision.reason, "the dice said stay quiet")

    def test_a_low_roll_speaks(self):
        with mock.patch("random.random", return_value=0.0):
            self.assertTrue(self.decide(config=self.open_config).posted)

    def test_proactive_prompt_is_an_operator_event(self):
        agent = StubAgent(text="今天先休息")
        bot = self.bot(agent=agent)
        decision = self.decide(bot=bot, config=self.open_config)
        self.assertTrue(decision.posted)
        self.assertEqual(agent.events[0][0].origin, "operator")

    def test_poke_only_window_never_calls_the_model(self):
        self.seed(count=1, age=900, content="[戳一戳] 戳了戳你")
        agent = StubAgent()
        decision = self.decide(bot=self.bot(agent=agent), config=self.open_config)
        self.assertEqual(decision.reason, "latest event is not a conversational turn")
        self.assertEqual(agent.events, [])

    def test_daily_quota_is_its_own_pool(self):
        self.store.activity.log_proactive(GROUP, "随机发言", "random")
        self.store.activity.log_proactive(GROUP, "随机发言", "random")
        self.store.activity.log_proactive(GROUP, "定时发言", "cron")
        self.assertEqual(
            self.decide(config=self.open_config).reason,
            "daily proactive limit reached",
        )
        # A cron/poster post does not spend the random pool: with room for
        # three, the same two random posts still leave room to speak.
        self.clear()
        roomier = cfg(quiet_minutes=10, probability=1.0, daily_limit=3)
        self.assertTrue(self.decide(config=roomier).posted)

    def test_the_bot_does_not_crowd_a_group_it_just_spoke_to(self):
        self.store.activity.log_proactive(GROUP, "刚说过", "random")
        self.assertEqual(
            self.decide(config=cfg(quiet_minutes=10, interval_minutes=180)).reason,
            "the bot spoke to this group too recently",
        )

    def test_a_reply_the_bot_just_sent_also_cools_it_down(self):
        self.engine(config=self.open_config).bot.last_reply[SCOPE] = time.time()
        bot = self.bot()
        bot.last_reply[SCOPE] = time.time()
        decision = self.decide(bot=bot, config=self.open_config)
        self.assertEqual(decision.reason, "the bot spoke to this group too recently")

    def test_a_withdrawn_mood_closes_the_gate(self):
        mood = StubMood(permits=False)
        agent = StubAgent()
        bot = self.bot(agent=agent, mood=mood)
        decision = self.decide(bot=bot, config=self.open_config, mood=mood)
        self.assertEqual(decision.reason, "not in the mood to start a conversation")
        self.assertEqual(mood.asked, [SCOPE])
        # The model was never bothered: the gate is checked first.
        self.assertEqual(agent.events, [])

    def test_a_sociable_mood_does_not_permit_the_not_yet_quiet_group(self):
        mood = StubMood(permits=True)
        self.seed(count=1, age=0, content="刚说完")
        bot = self.bot(mood=mood)
        decision = self.decide(bot=bot, config=self.open_config, mood=mood)
        self.assertEqual(decision.reason, "group is still talking")

    def test_the_model_gets_the_last_word(self):
        bot = self.bot(agent=StubAgent(text=""))
        decision = self.decide(bot=bot, config=self.open_config)
        self.assertEqual(decision.reason, "the model chose to stay silent")
        self.assertEqual(bot.sender.sent, [])
        # A silent decision is an attempt: it is remembered.
        self.assertEqual(self.state()["silent_streak"], 1)

    def test_a_repeated_line_is_rejected(self):
        bot = self.bot(agent=StubAgent(text="在吗"))
        decision = self.decide(bot=bot, config=self.open_config)
        self.assertEqual(decision.reason, "reply repeated a recent message")
        self.assertEqual(bot.sender.sent, [])

    def test_an_overlong_line_is_rejected(self):
        bot = self.bot(agent=StubAgent(text="啊" * 400))
        decision = self.decide(bot=bot, config=self.open_config)
        self.assertEqual(decision.reason, "reply longer than 150 characters")
        self.assertEqual(bot.sender.sent, [])

    def test_group_allowlist_is_a_hard_edge(self):
        bot = self.bot(allowed=("99",))
        self.assertEqual(
            self.decide(bot=bot, config=self.open_config).reason,
            "group is not allowlisted",
        )

    def test_outside_active_hours_nothing_happens(self):
        bot = self.bot(awake=False)
        self.assertEqual(
            self.decide(bot=bot, config=self.open_config).reason,
            "outside active hours",
        )

    def test_the_extension_switch_turns_the_engine_off(self):
        self.assertEqual(
            self.decide(config=cfg(enabled=False, quiet_minutes=10)).reason,
            "proactive chat is switched off",
        )

    def test_every_outcome_is_written_down_with_a_reason(self):
        self.decide(config=self.open_config)
        rows = ContinuationStore(self.store).decisions(GROUP)
        self.assertEqual(rows[0]["outcome"], "posted")
        self.assertEqual(rows[0]["reason"], "")
        self.decide(config=cfg(quiet_minutes=10))
        rows = ContinuationStore(self.store).decisions(GROUP)
        self.assertEqual(rows[0]["outcome"], "observed")
        self.assertEqual(rows[0]["reason"], "waiting for the group to speak again")


# --------------------------------------------------------------------------
# Per-group policy
# --------------------------------------------------------------------------


class GroupPolicyTests(ContinuationTestCase):
    def setUp(self):
        super().setUp()
        self.seed(age=900)

    def test_a_muted_group_is_never_continued_in(self):
        policies = GroupPolicySet({GROUP: GroupPolicy(enabled=False)})
        bot = self.bot()
        decision = self.decide(bot=bot, config=cfg(quiet_minutes=10), policies=policies)
        self.assertEqual(decision.reason, "group is muted")
        self.assertEqual(bot.sender.sent, [])

    def test_per_group_do_not_disturb_hours(self):
        policies = GroupPolicySet(
            {GROUP: GroupPolicy(quiet_start_hour=23, quiet_end_hour=8)}
        )
        night = self.bot(hour=3)
        self.assertEqual(
            self.decide(
                bot=night, config=cfg(quiet_minutes=10, probability=1.0), policies=policies
            ).reason,
            "inside the group's do-not-disturb hours",
        )
        day = self.bot(hour=12)
        self.assertTrue(
            self.decide(
                bot=day, config=cfg(quiet_minutes=10, probability=1.0), policies=policies
            ).posted
        )

    def test_a_group_override_beats_the_deployment_default(self):
        policies = GroupPolicySet({GROUP: GroupPolicy(daily_limit=0)})
        base = cfg(quiet_minutes=10, probability=1.0, daily_limit=5)
        self.assertEqual(
            self.decide(config=base, policies=policies).reason,
            "daily proactive limit reached",
        )
        # Without the override the same configuration posts.
        self.clear()
        self.assertTrue(self.decide(config=base).posted)

    def test_resolve_returns_none_for_a_muted_group(self):
        policies = GroupPolicySet({GROUP: GroupPolicy(enabled=False)})
        self.assertIsNone(policies.resolve(GROUP, cfg()))
        self.assertEqual(policies.resolve("99", cfg()).daily_limit, 2)

    def test_a_partial_entry_only_overrides_what_it_names(self):
        policies = GroupPolicySet({GROUP: GroupPolicy(quiet_minutes=45)})
        resolved = policies.resolve(GROUP, cfg(quiet_minutes=10, retry_minutes=7))
        self.assertEqual(resolved.quiet_minutes, 45)
        self.assertEqual(resolved.retry_minutes, 7)

    def test_policy_file_round_trip(self):
        path = self.root / "groups.json"
        path.write_text(
            json.dumps(
                {
                    "groups": {
                        GROUP: {
                            "enabled": True,
                            "quiet_start_hour": 23,
                            "quiet_end_hour": 8,
                            "probability": 0.25,
                            "daily_limit": 3,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        policies = GroupPolicySet.load(path)
        resolved = policies.resolve(GROUP, cfg(probability=0.5))
        self.assertEqual(resolved.probability, 0.25)
        self.assertEqual(resolved.daily_limit, 3)
        self.assertEqual(resolved.quiet_start_hour, 23)

    def test_a_missing_policy_file_is_empty_not_an_error(self):
        policies = GroupPolicySet.load(self.root / "nope.json")
        self.assertEqual(policies.groups, {})
        self.assertTrue(policies.get(GROUP).enabled)
        with self.assertRaisesRegex(ValueError, "not found"):
            GroupPolicySet.load(self.root / "nope.json", required=True)

    def test_a_malformed_policy_file_fails_at_startup(self):
        path = self.root / "groups.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            GroupPolicySet.load(path)

        path.write_text(json.dumps({"groups": {GROUP: {"enabeld": False}}}), "utf-8")
        with self.assertRaisesRegex(ValueError, "unknown keys"):
            GroupPolicySet.load(path)

        path.write_text(json.dumps({"groups": {GROUP: {"probability": 3}}}), "utf-8")
        with self.assertRaisesRegex(ValueError, "between 0.0 and 1.0"):
            GroupPolicySet.load(path)

        path.write_text(json.dumps({"groups": {GROUP: {"enabled": "yes"}}}), "utf-8")
        with self.assertRaisesRegex(ValueError, "true/false"):
            GroupPolicySet.load(path)

    def test_quiet_hours_wrap_midnight(self):
        self.assertTrue(in_quiet_hours(23, 8, 23))
        self.assertTrue(in_quiet_hours(23, 8, 3))
        self.assertFalse(in_quiet_hours(23, 8, 12))
        self.assertTrue(in_quiet_hours(13, 15, 14))
        self.assertFalse(in_quiet_hours(13, 15, 15))
        # Unconfigured or degenerate windows never silence anyone.
        self.assertFalse(in_quiet_hours(None, None, 3))
        self.assertFalse(in_quiet_hours(9, 9, 9))


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_defaults_are_permissive_for_programmatic_use(self):
        config = ProactiveConfig()
        self.assertTrue(config.enabled)
        self.assertEqual(config.quiet_minutes, 0)
        self.assertIsNone(config.quiet_start_hour)

    def test_environment_supplies_the_deployment_defaults(self):
        env = {
            "BOT_PROACTIVE_ENABLED": "false",
            "BOT_PROACTIVE_INTERVAL_MINUTES": "45",
            "BOT_PROACTIVE_DAILY_LIMIT": "1",
            "BOT_PROACTIVE_QUIET_MINUTES": "20",
            "BOT_PROACTIVE_FRESHNESS_MINUTES": "240",
            "BOT_PROACTIVE_RETRY_MINUTES": "15",
            "BOT_PROACTIVE_PROBABILITY": "0.4",
            "BOT_PROACTIVE_MIN_MESSAGES": "5",
            "BOT_PROACTIVE_MAX_CHARS": "80",
            "BOT_PROACTIVE_QUIET_START_HOUR": "22",
            "BOT_PROACTIVE_QUIET_END_HOUR": "9",
            "BOT_PROACTIVE_CHECK_SECONDS": "600",
            "BOT_PROACTIVE_GROUPS_PATH": "./config/custom.json",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            config = ProactiveConfig.from_env()
        self.assertFalse(config.enabled)
        self.assertEqual(config.interval_minutes, 45)
        self.assertEqual(config.daily_limit, 1)
        self.assertEqual(config.quiet_minutes, 20)
        self.assertEqual(config.freshness_minutes, 240)
        self.assertEqual(config.retry_minutes, 15)
        self.assertEqual(config.probability, 0.4)
        self.assertEqual(config.min_messages, 5)
        self.assertEqual(config.max_chars, 80)
        self.assertEqual(config.quiet_start_hour, 22)
        self.assertEqual(config.quiet_end_hour, 9)
        self.assertEqual(config.check_seconds, 600)
        self.assertEqual(str(config.groups_path), "config/custom.json")

    def test_unspecified_environment_uses_the_deployment_defaults(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            config = ProactiveConfig.from_env()
        self.assertEqual(config.interval_minutes, 180)
        self.assertEqual(config.quiet_minutes, 12)
        self.assertEqual(config.probability, 0.15)
        self.assertEqual((config.quiet_start_hour, config.quiet_end_hour), (23, 8))

    def test_the_check_interval_never_goes_below_the_scheduler_floor(self):
        with mock.patch.dict(
            "os.environ", {"BOT_PROACTIVE_CHECK_SECONDS": "30"}, clear=True
        ):
            self.assertEqual(ProactiveConfig.from_env().check_seconds, 300)

    def test_a_bad_value_is_rejected_rather_than_ignored(self):
        for name, value, message in (
            ("BOT_PROACTIVE_PROBABILITY", "2", "between 0 and 1"),
            ("BOT_PROACTIVE_QUIET_MINUTES", "lots", "must be an integer"),
            ("BOT_PROACTIVE_QUIET_START_HOUR", "99", "between 0 and 23"),
        ):
            with self.subTest(name=name):
                with mock.patch.dict("os.environ", {name: value}, clear=True):
                    with self.assertRaisesRegex(ValueError, message):
                        ProactiveConfig.from_env()


# --------------------------------------------------------------------------
# The scheduling seam
# --------------------------------------------------------------------------


class SchedulerIntegrationTests(ContinuationTestCase):
    """The acceptance criterion: outside the window and quiet groups are skipped."""

    def setUp(self):
        super().setUp()
        self.registry = JobHandlerRegistry()
        self.registry.register(ChatJobHandler())
        self.registry.register(
            ContinuationJobHandler(
                config=cfg(
                    quiet_minutes=10,
                    probability=1.0,
                    interval_minutes=0,
                    daily_limit=2,
                )
            )
        )
        self.scheduler = Scheduler(
            self.store.jobs, "Asia/Shanghai", self.registry.actions()
        )

    def schedules(self, jobs) -> Path:
        path = self.root / "schedules.json"
        path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        return path

    def enable_continuation(self):
        self.scheduler.sync_config(
            self.schedules(
                [
                    {
                        "id": f"continue-quiet@{GROUP}",
                        "group_id": GROUP,
                        "kind": "every",
                        "value": "300",
                        "action": "continuation",
                        "prompt": "群里安静下来了，有想说的就接一句。",
                    }
                ]
            ),
            frozenset({GROUP}),
            self.registry.suggested_jobs(),
        )
        self.store.db.execute(
            "UPDATE jobs SET next_run=0 WHERE config_key=?", (f"continue-quiet@{GROUP}",)
        )

    def run_due(self, bot):
        job = self.scheduler.reserve_due()[0]
        runner = JobRunner(bot, self.registry)
        asyncio.run(self.scheduler._execute(runner.run, job))
        return self.row("SELECT status, detail FROM job_runs WHERE id=?", (job["run_id"],))

    def test_the_suggestion_is_a_disabled_every_job(self):
        self.scheduler.sync_config(
            self.schedules([]), frozenset({GROUP}), self.registry.suggested_jobs()
        )
        job = self.row(
            "SELECT * FROM jobs WHERE config_key=?", (f"continue-quiet@{GROUP}",)
        )
        row = dict(job)
        self.assertEqual(row["action"], "continuation")
        self.assertEqual(row["schedule_kind"], "every")
        self.assertEqual(row["schedule_value"], "300")
        self.assertEqual(row["enabled"], 0)

    def test_a_group_still_talking_is_recorded_as_skipped(self):
        self.enable_continuation()
        self.seed(age=60)
        bot = self.bot()
        status, detail = self.run_due(bot)
        self.assertEqual(status, "skipped")
        self.assertEqual(detail, "group is still talking")
        self.assertEqual(bot.sender.sent, [])

    def test_a_quiet_group_is_continued_in_and_recorded_as_succeeded(self):
        self.enable_continuation()
        self.seed(age=900)
        bot = self.bot()
        status, detail = self.run_due(bot)
        self.assertEqual((status, detail), ("succeeded", ""))
        self.assertEqual([m["text"] for m in bot.sender.sent], ["收到。"])
        # The message went through the same bookkeeping as any other reply.
        self.assertEqual(
            self.store.activity.proactive_count_since(GROUP, 0, "random"), 1
        )
        self.assertTrue(
            any(
                row["role"] == "assistant"
                for row in self.store.conversations.recent(SCOPE, 5)
            )
        )

    def test_a_silent_model_is_recorded_as_skipped(self):
        self.enable_continuation()
        self.seed(age=900)
        status, detail = self.run_due(self.bot(agent=StubAgent(text="")))
        self.assertEqual(status, "skipped")
        self.assertEqual(detail, "the model chose to stay silent")

    def test_the_continuation_action_is_gated_by_mood(self):
        self.enable_continuation()
        self.seed(age=900)
        mood = StubMood(permits=False)
        bot = self.bot(mood=mood)
        status, detail = self.run_due(bot)
        self.assertEqual(status, "skipped")
        self.assertEqual(detail, "not in the mood to start a conversation")

    def test_the_scheduled_chat_action_is_not_gated_by_mood(self):
        """A deployer-requested post still lands when the bot is withdrawn."""
        self.scheduler.sync_config(
            self.schedules(
                [
                    {
                        "id": "water-noon",
                        "group_id": GROUP,
                        "kind": "cron",
                        "value": "30 12 * * *",
                        "prompt": "午休闲聊",
                    }
                ]
            ),
            frozenset({GROUP}),
            self.registry.suggested_jobs(),
        )
        self.store.db.execute("UPDATE jobs SET next_run=0 WHERE config_key='water-noon'")
        self.seed(age=60)
        mood = StubMood(permits=False)
        bot = self.bot(mood=mood)
        job = self.scheduler.reserve_due()[0]
        asyncio.run(self.scheduler._execute(JobRunner(bot, self.registry).run, job))
        status, _ = self.row(
            "SELECT status, detail FROM job_runs WHERE id=?", (job["run_id"],)
        )
        self.assertEqual(status, "succeeded")
        self.assertEqual([m["text"] for m in bot.sender.sent], ["收到。"])
        self.assertEqual(mood.asked, [])

    def test_register_jobs_wires_both_strategies(self):
        registry = JobHandlerRegistry()
        register_jobs(registry, SimpleNamespace(extensions=frozenset({"scheduled_chat"})))
        self.assertEqual(
            registry.actions(), frozenset({"chat", "deliver", "continuation"})
        )
        self.assertEqual(
            sorted(item.id for _, item in registry.suggested_jobs()),
            sorted(["continue-quiet", "water-noon", "water-night"]),
        )

    def test_a_handler_without_the_mood_module_still_works(self):
        """No mood extension means no gate, not a crash."""
        bot = self.bot()
        self.assertIsNone(mood_gate(bot))


class ImportBoundaryTests(unittest.TestCase):
    """The default configuration must not import the background extension."""

    def test_scheduled_chat_alone_does_not_import_the_tick_extension(self):
        import sys

        from qunbot.extensions.loader import build_registry

        sys.modules.pop("qunbot.extensions.proactive_chat.runner", None)
        build_registry(SimpleNamespace(extensions=frozenset({"scheduled_chat"})))
        self.assertNotIn("qunbot.extensions.proactive_chat.runner", sys.modules)


# --------------------------------------------------------------------------
# The background worker contract
# --------------------------------------------------------------------------


class WorkerContractTests(ContinuationTestCase):
    def test_build_worker_takes_the_four_argument_contract(self):
        bot = self.bot()
        gateway = SimpleNamespace(connection=None)
        worker = build_worker(bot, gateway, SimpleNamespace(extensions=frozenset()), None)
        self.assertTrue(asyncio.iscoroutine(worker))
        worker.close()

    def test_the_worker_picks_up_the_mood_observer_it_is_given(self):
        mood = StubMood()
        bot = self.bot(mood=mood)
        gateway = SimpleNamespace(connection=object())
        worker = build_worker(bot, gateway, SimpleNamespace(), mood)
        worker.close()
        self.assertIs(mood_gate(bot), mood)
        self.assertTrue(mood.permits_proactive(SCOPE))

    def test_local_hour_falls_back_to_the_policy_timezone(self):
        bot = SimpleNamespace(policy=SimpleNamespace(timezone="UTC"))
        self.assertEqual(local_hour(bot), local_hour(SimpleNamespace(policy=bot.policy)))


if __name__ == "__main__":
    unittest.main()
