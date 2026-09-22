from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from qunbot.runtime.agent import Agent
from qunbot.domain import JobSkipped, MessageEvent
from qunbot.adapters.events import parse_message
from qunbot.extensions.media import ReplyMediaProcessor
from qunbot.extensions.memes.catalog import LocalMemeCatalog
from qunbot.extensions.voice.sources import DashScopeSpeechSource
from qunbot.memory.service import MemoryService
from qunbot.extensions.exam_poster.job import PosterJobHandler
from qunbot.extensions.exam_poster.renderer import ExamCountdownPoster
from qunbot.extensions.scheduled_chat.job import ChatJobHandler
from qunbot.scheduling import JobHandlerRegistry, Scheduler, next_occurrence
from qunbot.scheduling.runner import JobRunner
from qunbot.runtime.service import BotPolicy, ConversationService
from qunbot.runtime.skills import SkillCatalog
from support import Store
from qunbot.runtime.tools import built_in_tools


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
            MemoryService(model, self.store, self.store),
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        service = ConversationService(
            agent,
            self.store,
            self.store,
            self.store,
            sender,
            BotPolicy(frozenset({"42"}), 8, "Asia/Shanghai", False),
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
            MemoryService(FakeModel(), self.store, self.store),
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        before = agent.stable_prefix()
        self.store.change_affection("42", "7", 2, "friendly interaction")
        after = agent.stable_prefix()
        self.assertEqual(before, after)
        content = agent.build_messages(event(1, at_bot=True))[-1]["content"]
        # The relationship reaches the model as a bounded, number-free stage
        # narration — never as the raw score. A bare integer invites the model
        # to reason about "the number" instead of the tone.
        self.assertNotIn('"affection"', content)
        narration = self.store.profile("42", "7")["relationship_note"]
        self.assertIn(narration, content)
        self.assertFalse(
            any(ch.isdigit() for ch in narration), f"narration leaked a score: {narration}"
        )

    def test_private_message_is_ignored_when_disabled(self):
        agent = Agent(
            FakeModel(),
            self.store,
            self.store,
            MemoryService(FakeModel(), self.store, self.store),
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        sender = FakeSender()
        service = ConversationService(
            agent,
            self.store,
            self.store,
            self.store,
            sender,
            BotPolicy(frozenset({"42"}), 8, "Asia/Shanghai", False),
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
        self.registry = JobHandlerRegistry()
        self.registry.register(ChatJobHandler())

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def policy(self, **overrides) -> BotPolicy:
        base = {
            "allowed_groups": frozenset({"42"}),
            "memory_extract_every": 8,
            "timezone": "Asia/Shanghai",
            "affection_auto_enabled": False,
            "private_enabled": False,
            "job_daily_limit": 6,
            # The tests must not depend on the wall clock hour.
            "active_start_hour": 0,
            "active_end_hour": 24,
            "job_cooldown_minutes": 30,
            "job_freshness_minutes": 180,
        }
        base.update(overrides)
        return BotPolicy(**base)

    def service(
        self, sender, *, posters=None, policy=None, handlers=None
    ) -> ConversationService:
        agent = Agent(
            FakeModel(),
            self.store,
            self.store,
            MemoryService(FakeModel(), self.store, self.store),
            SkillCatalog(self.root / "skills"),
            built_in_tools(self.store),
            self.persona,
        )
        registry = handlers or self.registry
        if posters is not None:
            registry = JobHandlerRegistry()
            registry.register(ChatJobHandler())
            registry.register(PosterJobHandler(posters))
        service = ConversationService(
            agent,
            self.store,
            self.store,
            self.store,
            sender,
            policy or self.policy(),
            None,
            None,
        )
        self.job_runner = JobRunner(service, registry)
        return service

    def job(self, **overrides) -> dict:
        base = {
            "id": 1,
            "run_id": 1,
            "group_id": "42",
            "action": "chat",
            "prompt": "接一句",
        }
        base.update(overrides)
        return base

    def test_poster_job_sends_rendered_image(self):
        sender = FakeSender()
        # A far-future exam date keeps the assertion independent of the clock.
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        service = self.service(
            sender, posters=ExamCountdownPoster(today + timedelta(days=365))
        )
        asyncio.run(self.job_runner.run(self.job(action="poster", prompt="")))
        self.assertEqual(len(sender.sent), 1)
        self.assertTrue(sender.sent[0]["image"].startswith("base64://"))
        self.assertTrue(sender.sent[0]["text"])
        self.assertEqual(self.store.proactive_count_since("42", 0, "poster"), 1)

    def test_poster_job_skips_after_exam_and_sends_nothing(self):
        sender = FakeSender()
        posters = ExamCountdownPoster(date(2020, 1, 1))
        service = self.service(sender, posters=posters)
        with self.assertRaises(JobSkipped):
            asyncio.run(self.job_runner.run(self.job(action="poster", prompt="")))
        self.assertEqual(sender.sent, [])

    def test_poster_job_without_renderer_is_skipped(self):
        service = self.service(FakeSender())
        with self.assertRaises(JobSkipped):
            asyncio.run(self.job_runner.run(self.job(action="poster", prompt="")))

    def test_chat_job_is_skipped_when_group_is_quiet(self):
        sender = FakeSender()
        service = self.service(sender)
        with self.assertRaises(JobSkipped):
            asyncio.run(self.job_runner.run(self.job()))
        self.assertEqual(sender.sent, [])

    def test_chat_job_posts_when_group_is_lively(self):
        self.store.add_message("m1", "group:42", "7", "小明", "user", "今天数学好难")
        sender = FakeSender()
        service = self.service(sender)
        asyncio.run(self.job_runner.run(self.job()))
        self.assertEqual([m["text"] for m in sender.sent], ["收到。"])
        self.assertEqual(self.store.proactive_count_since("42", 0, "cron"), 1)

    def test_chat_job_respects_cooldown_after_a_reply(self):
        self.store.add_message("m1", "group:42", "7", "小明", "user", "在吗")
        sender = FakeSender()
        service = self.service(sender)
        service.last_reply["group:42"] = time.time()
        with self.assertRaises(JobSkipped):
            asyncio.run(self.job_runner.run(self.job()))
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
        asyncio.run(self.job_runner.run(self.job()))
        with self.assertRaises(JobSkipped):
            asyncio.run(self.job_runner.run(self.job(run_id=2)))
        self.assertEqual(len(sender.sent), 1)

    def test_handlers_are_registered_explicitly(self):
        registry = JobHandlerRegistry()
        registry.register(ChatJobHandler())
        registry.register(PosterJobHandler(ExamCountdownPoster(date(2099, 1, 1))))
        self.assertEqual(registry.actions(), frozenset({"chat", "poster"}))
        self.assertEqual(
            sorted(item.id for _, item in registry.suggested_jobs()),
            ["kaoyan-daily-poster", "water-night", "water-noon"],
        )

    def test_a_suggested_job_is_seeded_disabled_and_promoted_by_config(self):
        """Dropping a handler file must never start posting on its own."""
        import json

        path = self.root / "schedules.json"
        path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        scheduler.sync_config(path, frozenset({"42"}), self.registry.suggested_jobs())
        seeded = {j["config_key"]: j for j in self.store.list_jobs("42")}
        self.assertIn("water-noon@42", seeded)
        self.assertEqual(seeded["water-noon@42"]["enabled"], 0)
        self.assertEqual(seeded["water-noon@42"]["created_by"], "handler")

        # Writing the same id into schedules.json claims it and turns it on.
        job = seeded["water-noon@42"]
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "water-noon@42",
                            "group_id": "42",
                            "kind": job["schedule_kind"],
                            "value": job["schedule_value"],
                            "prompt": "午休闲聊",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        scheduler.sync_config(path, frozenset({"42"}), self.registry.suggested_jobs())
        promoted = {j["config_key"]: j for j in self.store.list_jobs("42")}
        self.assertEqual(promoted["water-noon@42"]["enabled"], 1)
        self.assertEqual(promoted["water-noon@42"]["created_by"], "config")
        # Suggestions the operator did not claim stay off.
        self.assertEqual(promoted["water-night@42"]["enabled"], 0)

    def test_a_suggestion_duplicating_a_running_job_is_not_seeded(self):
        """Otherwise the operator sees their own schedule offered back to them."""
        import json

        path = self.root / "schedules.json"
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "my-own-lunch-job",
                            "group_id": "42",
                            "kind": "cron",
                            "value": "30 12 * * *",
                            "prompt": "午休闲聊",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        Scheduler(self.store, "Asia/Shanghai").sync_config(
            path, frozenset({"42"}), self.registry.suggested_jobs()
        )
        rows = {j["config_key"]: j for j in self.store.list_jobs("42")}
        self.assertIn("my-own-lunch-job", rows)
        # Same group + action + schedule, so the water-noon template is silent.
        self.assertNotIn("water-noon@42", rows)
        # A different schedule from the same handler is still offered.
        self.assertIn("water-night@42", rows)
        self.assertEqual(rows["water-night@42"]["enabled"], 0)

    def test_removing_a_handler_disables_its_suggestion(self):
        import json

        path = self.root / "schedules.json"
        path.write_text(json.dumps({"jobs": []}), encoding="utf-8")
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        scheduler.sync_config(path, frozenset({"42"}), self.registry.suggested_jobs())
        scheduler.sync_config(path, frozenset({"42"}), [])
        rows = {j["config_key"]: j for j in self.store.list_jobs("42")}
        self.assertEqual(rows["water-noon@42"]["enabled"], 0)

    def test_a_new_task_type_is_one_class_and_one_decorator(self):
        """A handler takes effect only after explicit registration."""
        from qunbot.scheduling import JobHandlerRegistry, suggestion

        class RollCallJob:
            action = "rollcall"

            def suggested_jobs(self):
                return [suggestion("daily-rollcall", "0 22 * * *", "提醒打卡")]

            async def run(self, bot, job):
                await bot.sender.send(
                    group_id=job["group_id"],
                    user_id=None,
                    text=f"打卡第 {job['run_id']} 天",
                )

        self.assertEqual(
            [s.id for s in RollCallJob().suggested_jobs()], ["daily-rollcall"]
        )

        registry = JobHandlerRegistry()
        registry.register(RollCallJob())
        sender = FakeSender()
        service = self.service(sender, handlers=registry)
        asyncio.run(self.job_runner.run(self.job(action="rollcall")))
        self.assertEqual([m["text"] for m in sender.sent], ["打卡第 1 天"])

        # Config validation follows the registry, so the new action is accepted
        # by sync_config only where the handler exists.
        import json

        path = self.root / "schedules.json"
        path.write_text(
            json.dumps(
                {
                    "jobs": [
                        {
                            "id": "r",
                            "group_id": "42",
                            "kind": "cron",
                            "value": "0 9 * * *",
                            "action": "rollcall",
                            "prompt": "打卡",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            Scheduler(self.store, "Asia/Shanghai").sync_config(path, frozenset({"42"}))
        Scheduler(self.store, "Asia/Shanghai", registry.actions()).sync_config(
            path, frozenset({"42"})
        )
        self.assertEqual(self.store.list_jobs("42")[0]["action"], "rollcall")

    def test_registry_rejects_duplicate_and_skips_unknown_actions(self):
        from qunbot.scheduling import JobHandlerRegistry

        registry = JobHandlerRegistry()
        registry.register(ChatJobHandler())
        with self.assertRaises(ValueError):
            registry.register(ChatJobHandler())
        self.assertEqual(registry.actions(), frozenset({"chat"}))
        with self.assertRaises(JobSkipped):
            registry.get("nope")

        sender = FakeSender()
        service = self.service(sender, handlers=self.registry)
        with self.assertRaises(JobSkipped):
            asyncio.run(self.job_runner.run(self.job(action="nonexistent")))
        self.assertEqual(sender.sent, [])

    def test_handler_cannot_escape_the_group_guard(self):
        """Guards live in one place, so no handler can bypass them."""
        from qunbot.scheduling import JobHandlerRegistry

        calls = []

        class RudeJob:
            action = "rude"

            async def run(self, bot, job):
                calls.append(job["id"])

        registry = JobHandlerRegistry()
        registry.register(RudeJob())
        sender = FakeSender()
        service = self.service(
            sender,
            handlers=registry,
            policy=self.policy(active_start_hour=0, active_end_hour=0),
        )
        with self.assertRaises(JobSkipped):
            asyncio.run(self.job_runner.run(self.job(action="rude")))
        self.assertEqual(calls, [])

    def test_scheduler_records_skipped_separately_from_failed(self):
        scheduler = Scheduler(self.store, "Asia/Shanghai")
        self.store.sync_jobs([("j", "42", "every", "300", "p", "chat", 0)], [], 0)
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
                # Only a poster may omit its caption prompt; absent means chat.
                "prompt": "早安" if action != "poster" else "",
            }
            if action:
                entry["action"] = action
            path.write_text(json.dumps({"jobs": [entry]}), encoding="utf-8")

        scheduler = Scheduler(self.store, "Asia/Shanghai", frozenset({"chat", "poster"}))
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
            Scheduler(self.store, "Asia/Shanghai").sync_config(path, frozenset({"42"}))

    def test_poster_renders_every_day_until_the_exam(self):
        poster = ExamCountdownPoster(date(2026, 12, 19))
        self.assertEqual(poster.days_left(date(2026, 12, 19)), 0)
        self.assertEqual(poster.days_left(date(2026, 12, 19) - timedelta(days=88)), 88)
        self.assertTrue(poster.render(date(2026, 12, 19)).startswith("base64://"))
        self.assertIsNone(poster.render(date(2026, 12, 20)))

    def test_poster_survives_a_multiline_model_caption(self):
        poster = ExamCountdownPoster(date(2026, 12, 19))
        # A real model caption looked like "坚持就是胜利！\n考研加油！" and the
        # renderer used to die here on Pillow's length measurement.
        self.assertTrue(
            poster.render(
                date(2026, 9, 22), motto="坚持就是胜利！\n考研加油！"
            ).startswith("base64://")
        )
        self.assertTrue(
            poster.render(date(2026, 9, 22), motto="   \n  ").startswith("base64://")
        )


if __name__ == "__main__":
    unittest.main()
