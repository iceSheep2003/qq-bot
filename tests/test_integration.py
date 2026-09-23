"""Cross-module wiring: the seams the parallel work could not test alone.

Each test here covers a fix made in the integration pass rather than inside a
single package — a port that was not passing a subject, a per-group fact that
was never recorded, environment parsing that had leaked into the adapters.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qunbot.adapters.events import parse_message
from qunbot.adapters.onebot import (
    DEFAULT_INBOUND_BACKLOG,
    DEFAULT_MAX_LANES,
    DEFAULT_REQUEST_TIMEOUT,
    OneBotGateway,
)
from qunbot.config import Config
from qunbot.domain import MessageEvent
from qunbot.runtime.tools import built_in_tools

from support import Store


def event(user_id: str, *, group_id: str = "42", card: str = "") -> MessageEvent:
    return MessageEvent(
        f"e{user_id}",
        f"group:{group_id}",
        group_id,
        user_id,
        "小明",
        "你好",
        (),
        True,
        (),
        0,
        card,
    )


class RecallMemoryScopeTests(unittest.TestCase):
    """``recall_memory`` must not hand one member another member's memories.

    Every group member shares a scope, so a subject-blind query returns
    everyone's personal rows. The tool has to pass the speaker through.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "bot.sqlite3")
        self.tools = built_in_tools(self.store.memories)

    def tearDown(self):
        self.temp.cleanup()

    def test_personal_memory_is_not_visible_to_another_member(self):
        self.store.memories.observe(
            "group:42", "7", "小明的生日是三月十二号", visibility="personal"
        )
        result = json.loads(
            self.tools.call("recall_memory", {"query": "生日"}, event("9"))
        )
        self.assertEqual(result, [])

    def test_personal_memory_is_visible_to_its_subject(self):
        self.store.memories.observe(
            "group:42", "7", "小明的生日是三月十二号", visibility="personal"
        )
        result = json.loads(
            self.tools.call("recall_memory", {"query": "生日"}, event("7"))
        )
        self.assertEqual([row["content"] for row in result], ["小明的生日是三月十二号"])

    def test_group_shared_memory_is_visible_to_every_member(self):
        self.store.memories.observe(
            "group:42", "7", "这个群在备考计算机统考", visibility="group"
        )
        result = json.loads(
            self.tools.call("recall_memory", {"query": "备考"}, event("9"))
        )
        self.assertEqual([row["content"] for row in result], ["这个群在备考计算机统考"])


class GroupCardTests(unittest.TestCase):
    """The group card is a per-group fact, not the global nickname."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "bot.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_parser_reads_the_card(self):
        parsed = parse_message(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "42",
                "user_id": "7",
                "self_id": "999",
                "message_id": "1",
                "message": [{"type": "text", "data": {"text": "hi"}}],
                "sender": {"nickname": "小明", "card": "三班-小明"},
            }
        )
        self.assertEqual(parsed.card, "三班-小明")
        self.assertEqual(parsed.nickname, "三班-小明")

    def test_parser_leaves_card_empty_when_absent(self):
        parsed = parse_message(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": "42",
                "user_id": "7",
                "self_id": "999",
                "message_id": "1",
                "message": [{"type": "text", "data": {"text": "hi"}}],
                "sender": {"nickname": "小明"},
            }
        )
        self.assertEqual(parsed.card, "")

    def test_card_is_scoped_to_its_group(self):
        self.store.people.observe_group_member("42", "7", "小明", "三班-小明")
        self.store.people.observe_group_member("99", "7", "小明", "五班-小明")
        self.assertEqual(self.store.people.profile("42", "7")["card"], "三班-小明")
        self.assertEqual(self.store.people.profile("99", "7")["card"], "五班-小明")

    def test_card_does_not_overwrite_the_global_nickname(self):
        self.store.people.observe_user("7", "小明")
        self.store.people.observe_group_member("42", "7", "小明", "三班-小明")
        self.assertEqual(self.store.people.profile("99", "7")["nickname"], "小明")


class ConfigParsingTests(unittest.TestCase):
    """config.Config is the single environment parsing point."""

    def test_concurrency_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()
        self.assertEqual(config.observer_queue_size, 256)
        self.assertEqual(config.observer_workers, 1)
        self.assertEqual(config.onebot_inbound_backlog, 64)
        self.assertEqual(config.onebot_max_lanes, 32)
        self.assertEqual(config.onebot_request_timeout, 20.0)

    def test_concurrency_overrides(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_OBSERVER_QUEUE_SIZE": "8",
                "BOT_OBSERVER_WORKERS": "3",
                "BOT_ONEBOT_INBOUND_BACKLOG": "5",
                "BOT_ONEBOT_MAX_LANES": "2",
                "BOT_ONEBOT_REQUEST_TIMEOUT": "1.5",
                "BOT_ONEBOT_MAX_FRAME_KB": "64",
            },
            clear=True,
        ):
            config = Config.from_env()
        self.assertEqual(config.observer_queue_size, 8)
        self.assertEqual(config.observer_workers, 3)
        self.assertEqual(config.onebot_inbound_backlog, 5)
        self.assertEqual(config.onebot_max_lanes, 2)
        self.assertEqual(config.onebot_request_timeout, 1.5)
        self.assertEqual(config.onebot_max_frame_kb, 64)


class AdapterEnvIsolationTests(unittest.TestCase):
    """Adapters take explicit arguments; they must not read os.environ."""

    def test_gateway_ignores_environment(self):
        with mock.patch.dict(
            os.environ,
            {
                "BOT_ONEBOT_INBOUND_BACKLOG": "7",
                "BOT_ONEBOT_MAX_LANES": "9",
                "BOT_ONEBOT_REQUEST_TIMEOUT": "3.0",
            },
            clear=True,
        ):
            gateway = OneBotGateway("127.0.0.1", 6199, "token")
        self.assertEqual(gateway.request_timeout, DEFAULT_REQUEST_TIMEOUT)
        self.assertEqual(gateway._dispatcher._backlog, DEFAULT_INBOUND_BACKLOG)
        self.assertEqual(gateway._dispatcher._max_keys, DEFAULT_MAX_LANES)

    def test_gateway_honours_explicit_arguments(self):
        gateway = OneBotGateway(
            "127.0.0.1",
            6199,
            "token",
            inbound_backlog=11,
            max_lanes=13,
            request_timeout=2.5,
            max_frame_bytes=2048,
        )
        self.assertEqual(gateway.request_timeout, 2.5)
        self.assertEqual(gateway._dispatcher._backlog, 11)
        self.assertEqual(gateway._dispatcher._max_keys, 13)
        self.assertEqual(gateway.max_frame_bytes, 2048)


ALL_EXTENSIONS = (
    "scheduled_chat,exam_poster,memes,mood,persona,reply_policy,"
    "slang,style_echo,world_context"
)


class AssemblyTests(unittest.TestCase):
    """The composition root: what app.py wires, in the order it wires it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                "BOT_ONEBOT_TOKEN": "x",
                "BOT_GROUP_ALLOWLIST": "866795853",
                "BOT_MODEL_API_KEY": "x",
                "BOT_EXAM_DATE": "2026-12-19",
                "BOT_DB_PATH": str(root / "bot.sqlite3"),
                "BOT_MOOD_DB_PATH": str(root / "emotion.sqlite3"),
                "BOT_EXTENSIONS": ALL_EXTENSIONS,
                "BOT_SLANG_ENABLED": "true",
                "BOT_PERSONA_ENABLED": "true",
                "BOT_WORLD_TIME_ENABLED": "true",
                "BOT_WORLD_REPLAY_ENABLED": "true",
                "BOT_REPLY_POLICY_ENABLED": "true",
            },
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def app(self):
        from qunbot.app import BotApp

        return BotApp(Config.from_env())

    def test_every_extension_can_be_enabled_at_once(self):
        app = self.app()
        self.assertEqual(
            sorted(app.features.context.names()),
            [
                "available_meme_tags",
                "group_slang",
                "memory_replay",
                "mood",
                "persona",
                "world_time",
            ],
        )

    def test_contributions_carry_their_declared_trust(self):
        app = self.app()
        trust = app.features.context.trust_map(event("7"))
        # A deployer's asset list and the local clock carry more authority
        # than anything a model produced.
        self.assertEqual(trust["available_meme_tags"], "high")
        self.assertEqual(trust["world_time"], "high")
        self.assertEqual(trust["mood"], "medium")
        self.assertEqual(trust["group_slang"], "medium")

    def test_reply_policy_is_installed_when_enabled(self):
        app = self.app()
        self.assertIsNotNone(app.service.reply_policy)

    def test_reply_policy_is_absent_when_switched_off(self):
        # Only the one knob changes; the rest of the deployment stays valid.
        with mock.patch.dict(os.environ, {"BOT_REPLY_POLICY_ENABLED": "false"}):
            app = self.app()
        self.assertIsNone(app.service.reply_policy)

    def test_feature_binders_ran_against_the_live_service(self):
        from qunbot.extensions import slang

        app = self.app()
        self.assertIs(slang._SERVICE, app.service)

    def test_shutdown_reaches_the_service(self):
        """run() drains observations before closing the database.

        Regression: aclose() was wired into run() while BotApp stopped holding
        the service, so shutdown raised AttributeError instead of draining.
        """
        app = self.app()
        self.assertTrue(hasattr(app, "service"))
        asyncio.run(app.service.aclose(timeout=1.0))

    def test_disabled_extensions_are_not_imported(self):
        """A disabled extension costs nothing: its module never loads."""
        import subprocess
        import sys

        code = (
            "import sys\n"
            "from qunbot.config import Config\n"
            "from qunbot.app import BotApp\n"
            "BotApp(Config.from_env())\n"
            "loaded = [m for m in sys.modules if 'extensions' in m]\n"
            "bad = [m for m in loaded if 'persona' in m or 'slang' in m "
            "or 'reply_policy' in m or 'world_context' in m or 'style_echo' in m]\n"
            "print('LEAKED:' + ','.join(bad))\n"
        )
        env = dict(os.environ)
        # Everything the schedules file needs, and none of the five new
        # packages — so a leak is unambiguous.
        env["BOT_EXTENSIONS"] = "scheduled_chat,exam_poster,memes,mood"
        env["BOT_DB_PATH"] = str(Path(self.temp.name) / "min.sqlite3")
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertIn("LEAKED:", result.stdout, result.stderr)
        self.assertEqual(result.stdout.strip(), "LEAKED:")


class ForgetUserTests(unittest.TestCase):
    """Cross-domain deletion must cover tables added after it was written."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "bot.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_forget_user_reaches_tables_it_was_never_told_about(self):
        from qunbot.storage.privacy import forget_user

        self.store.conversations.add_message("e1", "group:42", "7", "小明", "user", "hi")
        self.store.people.observe_user("7", "小明")
        self.store.people.observe_group_member("42", "7", "小明", "三班-小明")
        self.store.memories.remember("group:42", "7", "喜欢蓝莓蛋糕")

        removed = forget_user(self.store.database, "7")
        self.assertGreater(removed, 0)

        for table in ("messages", "people", "relations", "group_members", "memories"):
            with self.subTest(table=table):
                count = self.store.db.execute(
                    f"SELECT count(*) FROM {table} WHERE user_id=?", ("7",)
                ).fetchone()[0]
                self.assertEqual(count, 0)

    def test_forget_user_leaves_other_people_alone(self):
        from qunbot.storage.privacy import forget_user

        self.store.conversations.add_message("e1", "group:42", "7", "小明", "user", "hi")
        self.store.conversations.add_message("e2", "group:42", "9", "小红", "user", "hi")
        forget_user(self.store.database, "7")
        remaining = self.store.db.execute(
            "SELECT count(*) FROM messages WHERE user_id='9'"
        ).fetchone()[0]
        self.assertEqual(remaining, 1)


class DocumentedSettingsTests(unittest.TestCase):
    """A knob the deployer cannot discover is a knob that does not exist.

    Every BOT_* name read anywhere in the package must appear in
    .env.example, so adding a feature cannot quietly add an undocumented
    setting. The reverse is checked too: a documented name nothing reads is
    either a typo or a leftover.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def _code_names(self) -> set[str]:
        names: set[str] = set()
        for path in (self.ROOT / "qunbot").rglob("*.py"):
            names |= set(
                re.findall(r'"(BOT_[A-Z0-9_]+)"', path.read_text(encoding="utf-8"))
            )
        return names

    def _documented_names(self) -> set[str]:
        text = (self.ROOT / ".env.example").read_text(encoding="utf-8")
        return set(re.findall(r"^(BOT_[A-Z0-9_]+)=", text, re.M))

    def test_every_setting_read_is_documented(self):
        missing = self._code_names() - self._documented_names()
        self.assertEqual(sorted(missing), [], f"undocumented settings: {sorted(missing)}")

    def test_every_documented_setting_is_read(self):
        unused = self._documented_names() - self._code_names()
        self.assertEqual(sorted(unused), [], f"documented but unread: {sorted(unused)}")


if __name__ == "__main__":
    unittest.main()
