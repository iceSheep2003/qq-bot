from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

import httpx

from qunbot.agent import Agent
from qunbot.domain import JobSkipped, MessageEvent
from qunbot.events import parse_message
from qunbot.media import ReplyMediaProcessor
from qunbot.media_adapters import DashScopeSpeechSource, LocalMemeCatalog
from qunbot.poster import ExamCountdownPoster
from qunbot.scheduler import Scheduler, next_occurrence
from qunbot.service import BotPolicy, ConversationService
from qunbot.skills import SkillCatalog
from qunbot.store import Store
from qunbot.tools import built_in_tools


class FakeModel:
    async def complete(self, messages, tools=None, *, temperature=0.7):
        return {
            "choices": [{"message": {"content": "收到。"}}],
            "usage": {"prompt_tokens": 10},
        }


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return {}


def event(message_id: int, *, at_bot: bool) -> MessageEvent:
    return MessageEvent(
        str(message_id), "group:42", "42", "7", "小明", "你好", (), at_bot, (), 0
    )


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")
        self.persona = self.root / "persona.md"
        self.persona.write_text("固定人格", encoding="utf-8")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_onebot_parse_and_at(self):
        raw = {
            "post_type": "message",
            "message_type": "group",
            "message_id": 1,
            "self_id": 99,
            "user_id": 7,
            "group_id": 42,
            "sender": {"nickname": "小明"},
            "message": [
                {"type": "at", "data": {"qq": "99"}},
                {"type": "text", "data": {"text": "你好"}},
            ],
        }
        parsed = parse_message(raw)
        self.assertTrue(parsed.at_bot)
        self.assertEqual(parsed.scope, "group:42")

    def test_only_at_replies_and_chat_cannot_admin(self):
        model, sender = FakeModel(), FakeSender()
        agent = Agent(
            model,
            self.store,
            self.store,
            self.store,
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        service = ConversationService(
            agent,
            self.store,
            self.store,
            sender,
            BotPolicy(frozenset({"42"}), 8, False, 180, 2, "Asia/Shanghai", False),
        )

        async def run():
            await service.handle_message(event(1, at_bot=False))
            await service.handle_message(event(2, at_bot=True))
            await service.handle_message(event(2, at_bot=True))
            await service.handle_message(
                MessageEvent(
                    "3",
                    "group:42",
                    "42",
                    "7",
                    "小明",
                    "/task add every 300 刷屏",
                    (),
                    False,
                    (),
                    0,
                )
            )

        asyncio.run(run())
        self.assertEqual(len(sender.sent), 1)
        self.assertEqual(self.store.list_jobs("42"), [])

    def test_stable_prefix_excludes_dynamic_profile(self):
        agent = Agent(
            FakeModel(),
            self.store,
            self.store,
            self.store,
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        before = agent.stable_prefix()
        self.store.change_affection("42", "7", 2, "friendly interaction")
        after = agent.stable_prefix()
        self.assertEqual(before, after)
        self.assertIn(
            '"affection": 2', agent.build_messages(event(1, at_bot=True))[-1]["content"]
        )

    def test_private_message_is_ignored_when_disabled(self):
        agent = Agent(
            FakeModel(),
            self.store,
            self.store,
            self.store,
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        sender = FakeSender()
        service = ConversationService(
            agent,
            self.store,
            self.store,
            sender,
            BotPolicy(frozenset({"42"}), 8, False, 180, 2, "Asia/Shanghai", False),
        )
        private = MessageEvent(
            "private:1", "private:7", None, "7", "小明", "你好", (), False, (), 0
        )
        asyncio.run(service.handle_message(private))
        self.assertEqual(sender.sent, [])
        self.assertEqual(self.store.message_count("private:7"), 0)

    def test_memory_search_chinese(self):
        self.store.remember("group:42", "7", "小明喜欢蓝莓蛋糕")
        self.assertEqual(len(self.store.search_memories("group:42", "蓝莓蛋糕")), 1)

    def test_schedule_config_is_idempotent(self):
        import json

        path = self.root / "schedules.json"
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "morning",
                            "group_id": "42",
                            "kind": "cron",
                            "value": "0 9 * * *",
                            "prompt": "早安",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        scheduler.sync_config(path, frozenset({"42"}))
        first = scheduler.list("42")[0]
        scheduler.sync_config(path, frozenset({"42"}))
        second = scheduler.list("42")[0]
        self.assertEqual(
            (first["id"], first["next_run"]), (second["id"], second["next_run"])
        )
        self.assertGreater(
            next_occurrence("every", "300", after=100, tz_name="Asia/Shanghai"), 100
        )

    def test_past_one_shot_is_not_replayed(self):
        import json

        path = self.root / "schedules.json"
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "old",
                            "group_id": "42",
                            "kind": "at",
                            "value": "2020-01-01T09:00:00+08:00",
                            "prompt": "不要补发",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        scheduler.sync_config(path, frozenset({"42"}))
        scheduler.skip_missed()
        self.assertFalse(scheduler.list("42")[0]["enabled"])
        scheduler.sync_config(path, frozenset({"42"}))
        self.assertFalse(scheduler.list("42")[0]["enabled"])

    def test_meme_marker_is_removed_without_catalog_entry(self):
        catalog = LocalMemeCatalog(self.root / "memes")
        clean, image, voice = asyncio.run(
            ReplyMediaProcessor(catalog).compose("你好 [[meme:happy]] [[voice]]")
        )
        self.assertEqual((clean, image, voice), ("你好", None, None))

    def test_dashscope_tts_adapter(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={"output": {"audio": {"url": "http://audio.aliyuncs.com/1"}}},
                )
            return httpx.Response(200, content=b"audio")

        async def run():
            speech = DashScopeSpeechSource(
                "https://dashscope.aliyuncs.com/api/v1",
                "test-key",
                "qwen3-tts-flash",
                "Cherry",
            )
            await speech.client.aclose()
            speech.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            try:
                return await speech.synthesize("你好")
            finally:
                await speech.close()

        self.assertEqual(asyncio.run(run()), "base64://YXVkaW8=")
        self.assertEqual(
            requests[0].url.path,
            "/api/v1/services/aigc/multimodal-generation/generation",
        )
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
        self.assertEqual(str(requests[1].url), "https://audio.aliyuncs.com/1")


class ScheduleTests(unittest.TestCase):
    """Scheduled poster/chat jobs, their guards and their quota pools."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "bot.sqlite3")
        self.persona = self.root / "persona.md"
        self.persona.write_text("固定人格", encoding="utf-8")

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def policy(self, **overrides) -> BotPolicy:
        base = dict(
            allowed_groups=frozenset({"42"}),
            memory_extract_every=8,
            proactive_enabled=False,
            proactive_interval_minutes=180,
            proactive_daily_limit=2,
            timezone="Asia/Shanghai",
            affection_auto_enabled=False,
            private_enabled=False,
            job_daily_limit=6,
            # The tests must not depend on the wall clock hour.
            active_start_hour=0,
            active_end_hour=24,
            job_cooldown_minutes=30,
            job_freshness_minutes=180,
        )
        base.update(overrides)
        return BotPolicy(**base)

    def service(self, sender, *, posters=None, policy=None) -> ConversationService:
        agent = Agent(
            FakeModel(),
            self.store,
            self.store,
            self.store,
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        return ConversationService(
            agent,
            self.store,
            self.store,
            sender,
            policy or self.policy(),
            None,
            None,
            posters,
        )

    def job(self, **overrides) -> dict:
        base = {"id": 1, "run_id": 1, "group_id": "42", "action": "chat", "prompt": "接一句"}
        base.update(overrides)
        return base

    def test_poster_job_sends_rendered_image(self):
        sender = FakeSender()
        # A far-future exam date keeps the assertion independent of the clock.
        service = self.service(
            sender, posters=ExamCountdownPoster(date.today() + timedelta(days=365))
        )
        asyncio.run(service.run_job(self.job(action="poster", prompt="")))
        self.assertEqual(len(sender.sent), 1)
        self.assertTrue(sender.sent[0]["image"].startswith("base64://"))
        self.assertTrue(sender.sent[0]["text"])
        self.assertEqual(self.store.proactive_count_since("42", 0, "poster"), 1)

    def test_poster_job_skips_after_exam_and_sends_nothing(self):
        sender = FakeSender()
        posters = ExamCountdownPoster(date(2020, 1, 1))
        service = self.service(sender, posters=posters)
        with self.assertRaises(JobSkipped):
            asyncio.run(service.run_job(self.job(action="poster", prompt="")))
        self.assertEqual(sender.sent, [])

    def test_poster_job_without_renderer_is_skipped(self):
        service = self.service(FakeSender())
        with self.assertRaises(JobSkipped):
            asyncio.run(service.run_job(self.job(action="poster", prompt="")))

    def test_chat_job_is_skipped_when_group_is_quiet(self):
        sender = FakeSender()
        service = self.service(sender)
        with self.assertRaises(JobSkipped):
            asyncio.run(service.run_job(self.job()))
        self.assertEqual(sender.sent, [])

    def test_chat_job_posts_when_group_is_lively(self):
        self.store.add_message("m1", "group:42", "7", "小明", "user", "今天数学好难")
        sender = FakeSender()
        service = self.service(sender)
        asyncio.run(service.run_job(self.job()))
        self.assertEqual([m["text"] for m in sender.sent], ["收到。"])
        self.assertEqual(
            self.store.proactive_count_since("42", 0, "cron"), 1
        )

    def test_chat_job_respects_cooldown_after_a_reply(self):
        self.store.add_message("m1", "group:42", "7", "小明", "user", "在吗")
        sender = FakeSender()
        service = self.service(sender)
        service.last_reply["group:42"] = time.time()
        with self.assertRaises(JobSkipped):
            asyncio.run(service.run_job(self.job()))
        self.assertEqual(sender.sent, [])

    def test_quota_pools_do_not_starve_each_other(self):
        self.store.log_proactive("42", "随机主动", "random")
        self.store.log_proactive("42", "随机主动", "random")
        self.assertEqual(self.store.proactive_count_since("42", 0, "random"), 2)
        self.assertEqual(self.store.proactive_count_since("42", 0, "cron"), 0)
        self.assertEqual(self.store.proactive_count_since("42", 0), 2)

    def test_chat_job_stops_at_its_own_daily_limit(self):
        self.store.add_message("m1", "group:42", "7", "小明", "user", "在吗")
        sender = FakeSender()
        service = self.service(sender, policy=self.policy(job_daily_limit=1))
        asyncio.run(service.run_job(self.job()))
        with self.assertRaises(JobSkipped):
            asyncio.run(service.run_job(self.job(run_id=2)))
        self.assertEqual(len(sender.sent), 1)

    def test_scheduler_records_skipped_separately_from_failed(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        self.store.replace_config_jobs(
            [("j", "42", "every", "300", "p", "chat", 0)], 0
        )
        job = dict(self.store.list_jobs("42")[0])
        run_id = self.store.reserve_job(job, 999, 0)
        job["run_id"] = run_id

        async def decline(_job):
            raise JobSkipped("quiet hours")

        asyncio.run(scheduler._execute(decline, job))
        status = self.store.db.execute(
            "SELECT status, detail FROM job_runs WHERE id=?", (run_id,)
        ).fetchone()
        self.assertEqual((status[0], status[1]), ("skipped", "quiet hours"))

    def test_schedule_config_accepts_poster_and_rejects_unknown_action(self):
        import json

        path = self.root / "schedules.json"

        def write(action):
            entry = {
                "id": "p",
                "group_id": "42",
                "kind": "cron",
                "value": "0 7 * * *",
                "prompt": "",
            }
            if action:
                entry["action"] = action
            path.write_text(json.dumps({"jobs": [entry]}), encoding="utf-8")

        scheduler = Scheduler(self.store, "Asia/Shanghai")
        write("poster")
        scheduler.sync_config(path, frozenset({"42"}))
        self.assertEqual(scheduler.list("42")[0]["action"], "poster")

        write(None)
        scheduler.sync_config(path, frozenset({"42"}))
        self.assertEqual(scheduler.list("42")[0]["action"], "chat")

        write("explode")
        with self.assertRaises(ValueError):
            scheduler.sync_config(path, frozenset({"42"}))

    def test_chat_job_requires_a_nonempty_prompt(self):
        import json

        path = self.root / "schedules.json"
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "p",
                            "group_id": "42",
                            "kind": "cron",
                            "value": "0 7 * * *",
                            "prompt": "",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            Scheduler(self.store, "Asia/Shanghai").sync_config(
                path, frozenset({"42"})
            )

    def test_poster_renders_every_day_until_the_exam(self):
        poster = ExamCountdownPoster(date(2026, 12, 19))
        self.assertEqual(poster.days_left(date(2026, 12, 19)), 0)
        self.assertEqual(poster.days_left(date(2026, 12, 19) - timedelta(days=88)), 88)
        self.assertTrue(poster.render(date(2026, 12, 19)).startswith("base64://"))
        self.assertIsNone(poster.render(date(2026, 12, 20)))


if __name__ == "__main__":
    unittest.main()
