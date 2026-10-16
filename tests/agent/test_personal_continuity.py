"""Familiarity follows the current user; confidential context never follows it."""

import asyncio
import json
import time
from dataclasses import replace
from unittest.mock import patch

import pytest

from qqbot.agent.memory.continuity import PersonalContinuity
from qqbot.agent.service import ConversationService
from qqbot.bootstrap.app import BotApp
from qqbot.bootstrap.config import Config
from qqbot.bootstrap.runtime import close_sync
from qqbot.contracts.models import MessageEvent
from qqbot.infrastructure.storage.personal_memory import PersonalMemoryStore
from qqbot.infrastructure.storage.privacy import forget_user
from tests.agent.test_service import FakeAgent, FakeSender
from tests.support import Store, bot_policy


def event(user="7", *, group=None, text="想聊聊我喜欢的东西"):
    return MessageEvent("current", f"group:{group}" if group else f"private:{user}",
                        group, user, "测试用户", text, (), True, (), int(time.time()))


@pytest.fixture
def state(tmp_path):
    store = Store(tmp_path / "bot.sqlite3")
    try:
        yield store, PersonalMemoryStore(store.database)
    finally:
        store.database.close()


def remember(store, *, scope="group:42", user="7", content="喜欢蓝莓", **overrides):
    source = f"source-{scope}-{user}-{content}"
    store.add_message(source, scope, user, "测试用户", "user", content)
    options = dict(fact_type="preference", confidence=0.95, visibility="group",
                   source_event_id=source, origin_user_id=user)
    options.update(overrides)
    return store.memories.observe(scope, user, content, **options)["memory_id"]


def test_public_self_report_is_available_to_the_same_person_in_private(state):
    store, repo = state
    remember(store)
    context = PersonalContinuity(repo, frozenset({"42"})).context(event())
    assert [item["content"] for item in context["items"]] == ["喜欢蓝莓"]
    assert context["subject_user_id"] == "7"
    assert context["items"][0]["source_scope"] == "group:42"


@pytest.mark.parametrize("options", [
    {"scope": "group:43"}, {"scope": "private:7"}, {"user": "8"},
    {"fact_type": "relation"}, {"fact_type": "boundary"},
    {"visibility": "personal"}, {"mention_policy": "tone_only"},
    {"confidence": 0.4}, {"origin_user_id": "8"},
    {"source_event_id": "missing"}, {"expires_at": 1},
])
def test_private_other_people_and_unverified_or_restricted_facts_never_follow(state, options):
    store, repo = state
    remember(store)
    remember(store, content="不能共享的资料", **options)
    context = PersonalContinuity(repo, frozenset({"42"})).context(event())
    assert [item["content"] for item in context["items"]] == ["喜欢蓝莓"]


def test_groups_never_receive_private_continuity_context(state):
    store, repo = state
    remember(store)
    continuity = PersonalContinuity(repo, frozenset({"42"}))
    assert continuity.context(event())["items"]
    assert continuity.context(event(group="42")) == {}
    assert continuity.context(replace(event(), scope="private:8")) == {}


def test_context_is_bounded_and_deleted_source_is_not_recalled(state):
    store, repo = state
    ids = [remember(store, content=f"偏好{i}" + "蓝莓" * 100) for i in range(7)]
    continuity = PersonalContinuity(repo, frozenset({"42"}))
    context = continuity.context(event())
    assert len(context["items"]) <= 3
    assert len(json.dumps(context, ensure_ascii=False)) <= 1000
    assert context["items"]
    store.db.execute("UPDATE memories SET status='archived' WHERE id IN (%s)" % ",".join("?" for _ in ids), ids)
    assert continuity.context(event()).get("items", []) == []


def test_only_the_backend_can_disable_personal_continuity(state):
    store, repo = state
    remember(store)
    continuity = PersonalContinuity(repo, frozenset({"42"}))
    assert continuity.context(event())["items"]
    assert not hasattr(continuity, "control")
    repo.set_enabled("7", False)
    assert PersonalMemoryStore(store.database).enabled("7") is False
    assert continuity.context(event()) == {}
    repo.set_enabled("7", True)
    assert continuity.context(event())["items"]


def test_forgetting_user_removes_the_shared_source_and_preferences(state):
    store, repo = state
    remember(store)
    continuity = PersonalContinuity(repo, frozenset({"42"}))
    assert continuity.context(event())["items"]
    repo.set_enabled("7", False)
    forget_user(store.database, "7")
    assert repo.enabled("7") is True
    assert continuity.context(event()).get("items", []) == []


@pytest.mark.parametrize("text", ["关闭跨场景记忆", "开启跨场景记忆"])
def test_chat_switch_phrases_cannot_change_settings_or_bypass_quota(state, text):
    store, repo = state
    sender, agent = FakeSender(), FakeAgent()
    service = ConversationService(agent, store, store, store, sender,
        bot_policy(frozenset({"42"}), 8, "Asia/Shanghai", False, private_enabled=True))
    service.personal_continuity = PersonalContinuity(repo, frozenset({"42"}))
    class Denied:
        def admit(self, *args):
            return False
    service.private_admission = Denied()
    async def run():
        try:
            await service.handle_message(event(text=text))
            assert repo.enabled("7") is True
            assert agent.calls == []
            assert sender.sent == []
            assert store.message_count("private:7") == 0
        finally:
            await service.aclose()
    asyncio.run(run())


def test_chat_switch_phrase_is_an_ordinary_message_when_admitted(state):
    store, repo = state
    sender, agent = FakeSender(), FakeAgent()
    service = ConversationService(agent, store, store, store, sender,
        bot_policy(frozenset({"42"}), 8, "Asia/Shanghai", False, private_enabled=True))
    service.personal_continuity = PersonalContinuity(repo, frozenset({"42"}))
    async def run():
        try:
            await service.handle_message(event(text="关闭跨场景记忆"))
            assert repo.enabled("7") is True
            assert agent.calls == ["current"]
            assert store.message_count("private:7") > 0
            assert all("已关闭跨场景记忆" not in str(message) for message in sender.sent)
        finally:
            await service.aclose()
    asyncio.run(run())


def test_chat_instructions_do_not_advertise_memory_switches():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    text = (root / "skills/private-chat/SKILL.md").read_text()
    assert "关闭跨场景记忆" not in text
    assert "开启跨场景记忆" not in text


def test_bootstrap_contribution_is_dynamic_and_attributed_to_the_current_person(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("测试人格")
    schedules = tmp_path / "schedules.json"
    schedules.write_text('{"jobs": []}')
    with patch.dict("os.environ", {
        "BOT_ONEBOT_TOKEN": "test", "BOT_MODEL_API_KEY": "test", "BOT_GROUP_ALLOWLIST": "42",
        "BOT_PRIVATE_ENABLED": "true", "BOT_EXTENSIONS": "", "BOT_DB_PATH": str(tmp_path / "bot.sqlite3"),
        "BOT_PERSONA_PATH": str(persona), "BOT_SCHEDULES_PATH": str(schedules),
    }, clear=True):
        app = BotApp(Config.from_env())
    try:
        from qqbot.infrastructure.storage.memory import MemoryStore
        from qqbot.infrastructure.storage.conversation import ConversationStore
        from types import SimpleNamespace
        proxy = SimpleNamespace(memories=MemoryStore(app.database), add_message=ConversationStore(app.database).add_message)
        agent = app.service.agent
        prefix = agent.build_messages(event())[0]["content"]
        remember(proxy)
        data, trust = agent.context.collect_with_trust(event())
        assert "personal_continuity" in data
        assert data["personal_continuity"]["items"][0]["content"] == "喜欢蓝莓"
        assert trust["personal_continuity"] == "medium"
        assert agent.build_messages(event())[0]["content"] == prefix
        assert "喜欢蓝莓" in agent.build_messages(event())[-1]["content"]
        assert "喜欢蓝莓" not in agent.build_messages(event(group="42"))[0]["content"]
    finally:
        close_sync(app._closers)
