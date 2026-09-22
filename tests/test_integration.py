"""Cross-module wiring: the seams the parallel work could not test alone.

Each test here covers a fix made in the integration pass rather than inside a
single package — a port that was not passing a subject, a per-group fact that
was never recorded, environment parsing that had leaked into the adapters.
"""

from __future__ import annotations

import json
import os
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


if __name__ == "__main__":
    unittest.main()
