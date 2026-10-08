from __future__ import annotations

import unittest
import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from qunbot.adapters.events import parse_message
from qunbot.conversation_intelligence import ConversationIntelligence
from qunbot.conversation_compaction import ConversationCompactor
from qunbot.domain import MessageEvent
from qunbot.runtime.agent import Agent
from qunbot.runtime.skills import SkillCatalog
from qunbot.runtime.tools import ToolRegistry
from qunbot.storage.database import SqliteDatabase
from qunbot.storage.prompt_sessions import PromptSessionStore
from qunbot.storage.topic_observations import TopicObservationStore


class _Rows:
    def recent(self, scope, limit=18): return []


class _ActualDialogue:
    def recent(self, scope, limit=240):
        rows = []
        for i in range(30):
            rows.append({
                "event_id": f"e{i}", "role": "assistant" if i % 2 else "user",
                "nickname": "Bot" if i % 2 else "小明",
                "user_id": "bot" if i % 2 else "7",
                "content": f"实际对话{i}",
            })
        return rows[-limit:]


class _People:
    def profile(self, group_id, user_id): return {"relationship_note": ""}


class _Memories:
    def related(self, scope, query, limit=4): return []
    async def extract(self, scope): return None


class _RecordingModel:
    def __init__(self): self.requests = []
    async def complete(self, messages, tools=None, *, temperature=.7):
        self.requests.append(messages)
        number = len(self.requests)
        return {"choices": [{"message": {"role": "assistant", "content": f'{{"text":"r{number}","channels":["text"]}}'}}]}


class _CompactionModel:
    def __init__(self): self.requests = []
    async def complete(self, messages, tools=None, *, temperature=.7):
        self.requests.append(messages)
        content = json.dumps({
            "scene_summary": "大家在聊强化阶段",
            "active_threads": [{"summary": "择校", "participants": ["小明"]}],
            "open_loops": [{"summary": "等模考分数", "owner": "小明"}],
            "participant_stances": [],
            "callbacks": [{"summary": "赛博鸡蛋", "ttl_turns": 12}],
        }, ensure_ascii=False)
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class _InvalidCompactionModel:
    async def complete(self, messages, tools=None, *, temperature=.7):
        return {"choices": [{"message": {"role": "assistant", "content": "not json"}}]}


class _ConcurrentCompactionModel(_CompactionModel):
    def __init__(self, sessions):
        super().__init__()
        self.sessions = sessions

    async def complete(self, messages, tools=None, *, temperature=.7):
        self.sessions.append("group:42", [{"role": "user", "content": "刚到的新消息"}])
        return await super().complete(messages, tools, temperature=temperature)


def event(mid, user, text, *, ts, reply="", at=()):
    return MessageEvent(
        f"bot:{mid}", "group:42", "42", user, f"u{user}", text, (),
        "99" in at, tuple(at), ts, platform_message_id=str(mid),
        reply_to_message_id=str(reply),
    )


class ConversationIntelligenceTests(unittest.TestCase):
    def test_interleaved_topics_do_not_collapse_into_recent_n(self):
        ci = ConversationIntelligence()
        messages = (
            event(1, "1", "408数据结构怎么复习", ts=1),
            event(2, "2", "今晚英雄联盟开黑吗", ts=2),
            event(3, "3", "数据结构先看线性表", ts=3, reply="1"),
            event(4, "4", "开黑算我一个", ts=4, reply="2"),
            event(5, "1", "那组成原理呢", ts=5, reply="3", at=("99",)),
        )
        for item in messages:
            ci.observe(item)
        frame = ci.frame(messages[-1])
        topic_text = [item.text for item in frame.topic.messages]
        self.assertIn("408数据结构怎么复习", topic_text)
        self.assertIn("数据结构先看线性表", topic_text)
        self.assertNotIn("开黑算我一个", topic_text)
        self.assertEqual(frame.target.target_user_id, "3")
        self.assertEqual(frame.target.reasons, ("explicit_reply",))
        # Topic is background metadata. The live window preserves the actual
        # interleaved room scene so Grok can decide what the next line follows.
        self.assertEqual(
            [item.text for item in frame.recent_window],
            [item.text for item in messages],
        )
        payload = frame.prompt_payload()
        self.assertNotIn("messages", payload["topic_hint"])
        self.assertEqual(len(payload["recent_scene"]), 5)

    def test_short_reply_inherits_quoted_topic(self):
        ci = ConversationIntelligence()
        first = event(10, "1", "蓝莓蛋糕在哪里买", ts=10)
        second = event(11, "2", "这个呢", ts=30, reply="10", at=("99",))
        ci.observe(first)
        frame = ci.frame(second)
        self.assertEqual([m.platform_message_id for m in frame.topic.messages], ["10", "11"])

    def test_real_group_ellipsis_stays_with_immediately_preceding_topic(self):
        ci = ConversationIntelligence()
        first = event(40, "1", "用我的grok接进来么", ts=100)
        second = event(41, "1", "这样她就有无限的赛博鸡蛋", ts=102)
        third = event(42, "2", "看成ikun了", ts=104)
        for item in (first, second, third):
            ci.observe(item)
        frame = ci.frame(third)
        self.assertEqual(
            [m.text for m in frame.topic.messages],
            ["用我的grok接进来么", "这样她就有无限的赛博鸡蛋", "看成ikun了"],
        )

    def test_assistant_turn_is_a_first_class_part_of_the_topic(self):
        ci = ConversationIntelligence()
        question = event(50, "1", "这个怎么报名", ts=200)
        ci.observe(question)
        answer = replace(
            event(51, "bot", "先列三个目标院校", ts=201, reply="50"),
            nickname="Kinna", origin="assistant",
        )
        ci.observe(answer)
        follow = event(52, "1", "那第二个呢", ts=202, reply="51")
        frame = ci.frame(follow)
        self.assertEqual([m.origin for m in frame.topic.messages], ["human", "assistant", "human"])

    def test_operator_instruction_is_not_observed_as_group_chatter(self):
        ci = ConversationIntelligence()
        human = event(60, "1", "408怎么复习", ts=300)
        ci.observe(human)
        operator = replace(
            event(61, "bot", "根据群聊主动说一句", ts=301), origin="operator"
        )
        ci.observe(operator)
        frame = ci.latest_frame(human.scope)
        self.assertEqual([m.text for m in frame.topic.messages], ["408怎么复习"])

    def test_same_second_messages_keep_arrival_order_not_event_id_order(self):
        ci = ConversationIntelligence()
        first = event(90, "1", "艾特错了", ts=400)
        second = event(10, "2", "这个逻辑有点问题", ts=400, reply="90")
        ci.observe(first)
        frame = ci.frame(second)
        self.assertEqual([m.platform_message_id for m in frame.topic.messages], ["90", "10"])

    def test_recent_scene_resets_after_a_long_silence(self):
        ci = ConversationIntelligence(window_seconds=300)
        old = event(70, "1", "昨天那个学校怎么样", ts=100)
        current = event(71, "2", "有人打游戏吗", ts=1000)
        ci.observe(old)
        frame = ci.frame(current)
        self.assertEqual([m.text for m in frame.recent_window], ["有人打游戏吗"])

    def test_duplicate_observation_is_idempotent(self):
        ci = ConversationIntelligence()
        item = event(1, "1", "你好", ts=1)
        ci.observe(item)
        ci.observe(item)
        self.assertEqual(len(ci.frame(item).topic.messages), 1)

    def test_onebot_reply_segment_is_preserved(self):
        parsed = parse_message({
            "post_type": "message", "message_type": "group", "message_id": 8,
            "self_id": 99, "user_id": 7, "group_id": 42, "time": 1,
            "sender": {"nickname": "小明"},
            "message": [
                {"type": "reply", "data": {"id": "6", "user_id": "5"}},
                {"type": "at", "data": {"qq": "99"}},
                {"type": "text", "data": {"text": "这个呢"}},
            ],
        })
        self.assertEqual(parsed.platform_message_id, "8")
        self.assertEqual(parsed.reply_to_message_id, "6")
        self.assertEqual(parsed.reply_to_user_id, "5")

    def test_prompt_session_commits_only_after_successful_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persona = root / "persona.md"
            persona.write_text("固定人格", encoding="utf-8")
            db = SqliteDatabase(root / "bot.sqlite3")
            sessions = PromptSessionStore(db)
            model = _RecordingModel()
            agent = Agent(
                model, _Rows(), _People(), _Memories(), SkillCatalog(root / "skills"),
                ToolRegistry(), persona, prompt_sessions=sessions,
            )
            first = event(20, "1", "第一问", ts=20, at=("99",))
            second = event(21, "1", "第二问", ts=21, at=("99",))
            reply = asyncio.run(agent.reply(first))
            self.assertEqual(sessions.load(first.scope), [])
            agent.commit_delivered_reply(reply, first, "实际发出的第一答")
            committed = sessions.load(first.scope)
            self.assertEqual(committed[-1], {
                "role": "assistant", "content": "实际发出的第一答"
            })
            asyncio.run(agent.reply(second))
            second_request = model.requests[1]
            self.assertEqual(second_request[1:3], committed)
            self.assertNotIn("本轮动态上下文", second_request[1]["content"])
            db.close()

    def test_live_session_omits_stale_topic_but_keeps_stored_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persona = root / "persona.md"
            persona.write_text("固定人格", encoding="utf-8")
            db = SqliteDatabase(root / "bot.sqlite3")
            sessions = PromptSessionStore(db)
            sessions.append("group:42", [
                {"role": "user", "content": "旧话题：DS 是数据结构"},
                {"role": "assistant", "content": "旧回答"},
            ])
            db.db.execute(
                "UPDATE prompt_session_events SET created_at=created_at-7200 "
                "WHERE scope='group:42'"
            )
            sessions.append("group:42", [
                {"role": "user", "content": "刚才说的是模型"},
                {"role": "assistant", "content": "我刚才理解错了"},
            ])
            agent = Agent(
                _RecordingModel(), _Rows(), _People(), _Memories(),
                SkillCatalog(root / "skills"), ToolRegistry(), persona,
                prompt_sessions=sessions,
            )
            request = agent.build_messages(event(22, "1", "之前是 DS", ts=22, at=("99",)))
            history = request[1:-1]
            self.assertEqual([item["content"] for item in history], [
                "刚才说的是模型", "我刚才理解错了",
            ])
            self.assertEqual(len(sessions.load("group:42")), 4)
            db.close()

    def test_topic_index_rebuilds_after_restart_without_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SqliteDatabase(Path(directory) / "bot.sqlite3")
            store = TopicObservationStore(db)
            first = ConversationIntelligence(observations=store)
            root = event(31, "1", "408数据结构", ts=31)
            first.observe(root)
            restored = ConversationIntelligence(observations=store)
            follow = event(32, "2", "这个呢", ts=32, reply="31", at=("99",))
            frame = restored.frame(follow)
            self.assertEqual([m.platform_message_id for m in frame.topic.messages], ["31", "32"])
            db.close()

    def test_grok_compaction_is_one_explicit_series_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SqliteDatabase(Path(directory) / "bot.sqlite3")
            sessions = PromptSessionStore(db)
            sessions.append("group:42", [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
                for i in range(81)
            ])
            model = _CompactionModel()
            compactor = ConversationCompactor(model, sessions)
            self.assertTrue(asyncio.run(compactor.compact_if_needed("group:42")))
            compacted = sessions.load("group:42")
            self.assertEqual(len(compacted), 25)
            self.assertIn("会话压缩快照", compacted[0]["content"])
            self.assertIn("强化阶段", compacted[0]["content"])
            self.assertFalse(asyncio.run(compactor.compact_if_needed("group:42")))
            self.assertEqual(len(model.requests), 1)
            db.close()

    def test_compaction_uses_actual_dialogue_not_dynamic_request_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SqliteDatabase(Path(directory) / "bot.sqlite3")
            sessions = PromptSessionStore(db)
            sessions.append("group:42", [
                {"role": "user", "content": (
                    "本轮动态上下文：" + "记忆技能情绪表情列表" * 100
                    + f"\n\n当前消息：小明: 问题{i}"
                )}
                for i in range(20)
            ])
            model = _CompactionModel()
            compactor = ConversationCompactor(
                model, sessions, _ActualDialogue(),
                max_messages=100, max_chars=100, tail_messages=6,
            )
            self.assertTrue(asyncio.run(compactor.compact_if_needed("group:42")))
            request_text = model.requests[0][1]["content"]
            self.assertIn("实际对话0", request_text)
            self.assertNotIn("记忆技能情绪表情列表", request_text)
            compacted = sessions.load("group:42")
            self.assertEqual(len(compacted), 7)
            self.assertEqual(compacted[-1]["content"], "实际对话29")
            db.close()

    def test_invalid_grok_compaction_keeps_every_original_message(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SqliteDatabase(Path(directory) / "bot.sqlite3")
            sessions = PromptSessionStore(db)
            messages = [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
                for i in range(81)
            ]
            sessions.append("group:42", messages)
            compactor = ConversationCompactor(_InvalidCompactionModel(), sessions)
            self.assertFalse(asyncio.run(compactor.compact_if_needed("group:42")))
            self.assertEqual(sessions.load("group:42"), messages)
            db.close()

    def test_stale_grok_result_cannot_overwrite_a_new_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SqliteDatabase(Path(directory) / "bot.sqlite3")
            sessions = PromptSessionStore(db)
            sessions.append("group:42", [
                {"role": "user", "content": "x" * 20} for _ in range(20)
            ])
            model = _ConcurrentCompactionModel(sessions)
            compactor = ConversationCompactor(
                model, sessions, max_messages=100, max_chars=10, tail_messages=4
            )
            self.assertFalse(asyncio.run(compactor.compact_if_needed("group:42")))
            self.assertEqual(sessions.load("group:42")[-1]["content"], "刚到的新消息")
            db.close()

    def test_legacy_operator_prompt_and_result_are_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SqliteDatabase(Path(directory) / "bot.sqlite3")
            sessions = PromptSessionStore(db)
            sessions.append("group:42", [
                {"role": "user", "content": "正常问题"},
                {"role": "assistant", "content": "正常回答"},
                {"role": "user", "content": (
                    '本轮动态上下文：{\"当前场景\": \"主动群聊\", '
                    '\"当前发言人\": {\"id\": \"bot\", \"nickname\": \"Bot\"}}'
                )},
                {"role": "assistant", "content": "定时任务结果"},
            ])
            self.assertEqual(
                [m["content"] for m in sessions.load("group:42")],
                ["正常问题", "正常回答"],
            )
            db.close()


if __name__ == "__main__":
    unittest.main()
