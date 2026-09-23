"""The composition root: what it refuses, and what it cleans up when it fails.

A half-built ``BotApp`` used to strand whatever it had already opened, so a
deployment with one bad extension leaked a SQLite connection and an HTTP client
on every restart attempt. These tests open resources for real (no network: the
model client only holds an unstarted ``httpx.AsyncClient``) and record the
teardown order.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from qunbot.app import _aclose_all, _close_sync
from qunbot.config import CONFIG_VERSION, Config
from qunbot.domain import (
    BotPolicy,
    ConfigError,
    ContextFragment,
    ConversationPolicy,
    SchedulePolicy,
)
from qunbot.runtime.context import ContextRegistry

ROOT = Path(__file__).resolve().parent.parent


def event(user_id: str = "7"):
    from qunbot.domain import MessageEvent

    return MessageEvent(
        f"e{user_id}", "group:42", "42", user_id, "小明", "你好", (), True, (), 0
    )


class _CloseRecorder:
    """Forwards everything to a real sqlite connection, recording the close."""

    def __init__(self, connection, name, events):
        self._connection, self._name, self._events = connection, name, events

    def close(self):
        self._events.append(self._name)
        self._connection.close()

    def __getattr__(self, item):
        return getattr(self._connection, item)


class AssemblyBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events: list[str] = []

    def tearDown(self):
        self.temp.cleanup()

    def base_env(self, **overrides) -> dict:
        env = {
            "BOT_ONEBOT_TOKEN": "x",
            "BOT_GROUP_ALLOWLIST": "42",
            "BOT_MODEL_API_KEY": "x",
            "BOT_DB_PATH": str(self.root / "bot.sqlite3"),
            "BOT_SCHEDULES_PATH": str(self._schedules()),
            "BOT_PERSONA_PATH": str(self._persona()),
        }
        env.update(overrides)
        return env

    def _schedules(self) -> Path:
        path = self.root / "schedules.json"
        path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        return path

    def _persona(self) -> Path:
        path = self.root / "persona.md"
        path.write_text("固定人格", encoding="utf-8")
        return path

    def _instrumented(self):
        """Patch the two resources whose teardown we assert on."""
        import qunbot.app as app_module

        events = self.events
        real_database, real_model = app_module.SqliteDatabase, app_module.ModelClient

        def make_database(path):
            database = real_database(path)
            database.db = _CloseRecorder(database.db, "database", events)
            return database

        class RecordingModel(real_model):
            async def close(self):
                events.append("model")
                await super().close()

        return (
            mock.patch.object(app_module, "SqliteDatabase", make_database),
            mock.patch.object(app_module, "ModelClient", RecordingModel),
        )

    def build(self, **overrides):
        from qunbot.app import BotApp

        with mock.patch.dict(os.environ, self.base_env(**overrides), clear=True):
            return BotApp(Config.from_env())


class StartupRefusalTests(AssemblyBase):
    """Bad configuration must fail before anything is opened."""

    def test_missing_token_is_refused(self):
        with mock.patch.dict(
            os.environ,
            self.base_env(BOT_ONEBOT_TOKEN=""),
            clear=True,
        ):
            config = Config.from_env()
        from qunbot.app import BotApp

        with self.assertRaises(ConfigError) as caught:
            BotApp(config)
        self.assertEqual(str(caught.exception), "BOT_ONEBOT_TOKEN is required")
        self.assertFalse((self.root / "bot.sqlite3").exists())

    def test_unknown_extension_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.build(BOT_EXTENSIONS="nope")
        self.assertIn("unknown extensions", str(caught.exception))

    def test_missing_extension_config_is_refused(self):
        """voice enabled without its four settings fails at startup."""
        with self.assertRaises(ValueError) as caught:
            self.build(BOT_EXTENSIONS="voice")
        self.assertIn("BOT_TTS_", str(caught.exception))

    def test_duplicate_action_is_refused(self):
        from qunbot.scheduling import JobHandlerRegistry

        registry = JobHandlerRegistry()

        class Handler:
            action = "chat"

            def suggested_jobs(self):
                return []

            async def run(self, bot, job):  # pragma: no cover - never dispatched
                raise AssertionError

        registry.register(Handler())
        with self.assertRaises(ValueError) as caught:
            registry.register(Handler())
        self.assertEqual(str(caught.exception), "duplicate job action: chat")


class AssemblyCleanupTests(AssemblyBase):
    """A failed assembly must not strand a database connection or a client."""

    def test_failure_closes_the_database_and_the_model_client(self):
        database_patch, model_patch = self._instrumented()
        with database_patch, model_patch:
            with self.assertRaises(ValueError):
                self.build(BOT_EXTENSIONS="voice")
        self.assertEqual(self.events, ["model", "database"])

    def test_failure_rolls_back_everything_opened_so_far(self):
        database_patch, model_patch = self._instrumented()
        with database_patch, model_patch:
            with self.assertRaises(ValueError):
                # Unknown extensions are rejected inside build_features, i.e.
                # after the database and the model client exist.
                self.build(BOT_EXTENSIONS="not_a_real_extension")
        self.assertEqual(self.events, ["model", "database"])

    def test_a_successful_assembly_closes_nothing(self):
        database_patch, model_patch = self._instrumented()
        with database_patch, model_patch:
            app = self.build()
        try:
            self.assertEqual(self.events, [])
            self.assertTrue(hasattr(app, "service"))
        finally:
            # Do not leak the connection this test deliberately kept open.
            app.database.db.close()

    def test_a_failed_assembly_does_not_reuse_a_closed_connection(self):
        """The rollback closes each resource exactly once."""
        database_patch, model_patch = self._instrumented()
        with database_patch, model_patch:
            with self.assertRaises(ValueError):
                self.build(BOT_EXTENSIONS="voice")
        self.assertEqual(len(self.events), len(set(self.events)))


class TeardownHelperTests(unittest.TestCase):
    """Reverse order, best-effort isolation, sync and async steps alike."""

    def test_aclose_all_unwinds_in_reverse_and_isolates_failures(self):
        events: list[str] = []

        async def async_close():
            events.append("async")

        def bad():
            raise RuntimeError("boom")

        def sync_close():
            events.append("sync")

        with self.assertLogs("qunbot.app", level="ERROR"):
            asyncio.run(_aclose_all([sync_close, bad, async_close]))
        self.assertEqual(events, ["async", "sync"])

    def test_close_sync_unwinds_in_reverse_and_isolates_failures(self):
        events: list[str] = []

        def bad():
            raise RuntimeError("boom")

        with self.assertLogs("qunbot.app", level="ERROR"):
            _close_sync([lambda: events.append("first"), bad, lambda: events.append("last")])
        self.assertEqual(events, ["last", "first"])

    def test_close_sync_settles_an_async_step(self):
        events: list[str] = []

        async def async_close():
            events.append("async")

        _close_sync([lambda: events.append("sync"), async_close])
        self.assertEqual(events, ["async", "sync"])


class PolicySplitTests(unittest.TestCase):
    """The composite policy stays drop-in for its duck-typed consumers."""

    def policy(self) -> BotPolicy:
        return BotPolicy(
            conversation=ConversationPolicy(
                allowed_groups=frozenset({"42"}),
                memory_extract_every=8,
                timezone="Asia/Shanghai",
                affection_auto_enabled=False,
                private_enabled=True,
            ),
            schedule=SchedulePolicy(
                daily_limit=3,
                active_start_hour=0,
                active_end_hour=24,
                cooldown_minutes=15,
                freshness_minutes=60,
                max_chars=100,
            ),
        )

    def test_every_legacy_name_still_resolves(self):
        """runtime/service.py and scheduling/guards.py read these by name."""
        policy = self.policy()
        for name in (
            "allowed_groups",
            "memory_extract_every",
            "timezone",
            "affection_auto_enabled",
            "private_enabled",
            "job_daily_limit",
            "active_start_hour",
            "active_end_hour",
            "job_cooldown_minutes",
            "job_freshness_minutes",
            "job_max_chars",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(policy, name), name)

    def test_each_name_maps_to_its_own_half(self):
        policy = self.policy()
        self.assertEqual(policy.allowed_groups, policy.conversation.allowed_groups)
        self.assertEqual(policy.job_daily_limit, policy.schedule.daily_limit)
        self.assertEqual(policy.active_end_hour, policy.schedule.active_end_hour)

    def test_the_policy_is_frozen(self):
        policy = self.policy()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.conversation = None  # type: ignore[misc]

    def test_conversation_service_accepts_the_composite(self):
        from qunbot.runtime.service import ConversationService

        service = ConversationService(
            None,
            None,
            None,
            None,
            None,
            self.policy(),
            restore_last_reply=False,
        )
        self.assertEqual(service.policy.allowed_groups, frozenset({"42"}))
        self.assertTrue(service.within_active_hours())


class ContextFragmentTests(unittest.TestCase):
    """The documented fragment shape matches what the registry produces."""

    def test_registry_contributions_satisfy_the_protocol(self):
        registry = ContextRegistry()
        registry.register("greeting", lambda ev: "你好", max_chars=64)
        registry.register("broken", lambda ev: 1 / 0)

        # The broken contributor is expected to log; keep the run quiet.
        with self.assertLogs("qunbot.runtime.context", level="ERROR"):
            fragments = registry.contributions(event())
        self.assertEqual([f.name for f in fragments], ["greeting", "broken"])
        for fragment in fragments:
            with self.subTest(name=fragment.name):
                self.assertIsInstance(fragment.name, str)
                self.assertIsInstance(fragment.trust, int)
                self.assertIsInstance(fragment.priority, int)
                self.assertIsInstance(fragment.max_chars, int)
                self.assertIsInstance(fragment.failed, bool)
                self.assertTrue(callable(fragment.rendered))

        self.assertTrue(fragments[1].failed)
        self.assertIsNone(fragments[1].rendered())
        # A Protocol is not runtime-checkable, so conform structurally instead.
        fragment = fragments[0]
        for attribute in ContextFragment.__annotations__:
            self.assertTrue(hasattr(fragment, attribute), attribute)


class CheckOutputTests(AssemblyBase):
    """``--check`` is JSON consumed by tooling; its keys are a contract."""

    def check(self) -> dict:
        from qunbot.app import main

        buffer = io.StringIO()
        env = self.base_env(BOT_SKILLS_PATH=str(ROOT / "skills"))
        env.pop("BOT_MODEL_API_KEY")  # --check must not need the key
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            sys, "argv", ["qunbot", "--check"]
        ), redirect_stdout(buffer):
            main()
        return json.loads(buffer.getvalue())

    def test_check_reports_the_config_version(self):
        payload = self.check()
        self.assertEqual(payload["config_version"], CONFIG_VERSION)

    def test_check_keeps_its_existing_fields(self):
        payload = self.check()
        self.assertEqual(
            sorted(payload),
            [
                "actions",
                "config_version",
                "enabled_jobs",
                "extensions",
                "features",
                "groups",
                "model",
                "skills",
                "to_enable",
            ],
        )

    def test_check_runs_without_a_model_key(self):
        self.assertEqual(self.check()["groups"], ["42"])


if __name__ == "__main__":
    unittest.main()
