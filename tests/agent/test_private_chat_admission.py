"""Durable daily distinct-user admission before any private-chat model work."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from unittest.mock import patch
import asyncio

import pytest

from qqbot.bootstrap.config import Config
from qqbot.contracts.models import ConfigError, MessageEvent
from qqbot.agent.service import ConversationService
from qqbot.infrastructure.storage.private_chat import PrivateChatQuota
from qqbot.infrastructure.storage.database import SqliteDatabase
from tests.support import Store, bot_policy
from tests.agent.test_service import FakeAgent, FakeSender


def test_daily_distinct_quota_survives_restart_and_resets_on_next_day(tmp_path):
    path = tmp_path / 'bot.sqlite3'
    db = SqliteDatabase(path)
    try:
        quota = PrivateChatQuota(db, daily_limit=1)
        assert quota.admit('7', '2026-10-16')
        assert quota.admit('7', '2026-10-16')
        assert not quota.admit('8', '2026-10-16')
    finally:
        db.close()
    db = SqliteDatabase(path)
    try:
        quota = PrivateChatQuota(db, daily_limit=1)
        assert not quota.admit('8', '2026-10-16')
        assert quota.admit('8', '2026-10-17')
        assert not quota.admit('7', '2026-10-17')
    finally:
        db.close()


def test_atomic_admission_does_not_oversubscribe_distinct_user_limit(tmp_path):
    db = SqliteDatabase(tmp_path / 'bot.sqlite3')
    try:
        quota = PrivateChatQuota(db, daily_limit=3)
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(lambda user: quota.admit(str(user), '2026-10-16'), range(20)))
        assert sum(accepted) == 3
    finally:
        db.close()


def test_zero_limit_disables_private_admission(tmp_path):
    db = SqliteDatabase(tmp_path / 'bot.sqlite3')
    try:
        quota = PrivateChatQuota(db, daily_limit=0)
        assert quota.admit('7', '2026-10-16') is False
    finally:
        db.close()


def test_limit_setting_has_a_bounded_default_and_rejects_invalid_values():
    with patch.dict('os.environ', {}, clear=True):
        assert getattr(Config.from_env(), 'private_daily_user_limit', None) == 10
    for value in ('-1', '1001', 'bad'):
        with patch.dict('os.environ', {'BOT_PRIVATE_DAILY_USER_LIMIT': value}, clear=True):
            with pytest.raises((ConfigError, ValueError)):
                Config.from_env()


def test_denied_private_user_never_enters_ledger_media_guard_or_model(tmp_path):
    async def scenario():
        store = Store(tmp_path / 'bot.sqlite3')
        agent = FakeAgent()
        service = ConversationService(agent, store, store, store, FakeSender(),
            bot_policy(frozenset({'42'}), 8, 'Asia/Shanghai', False, private_enabled=True))
        service.private_admission = PrivateChatQuota(store.database, daily_limit=1)
        service.local_today = lambda: date(2026, 10, 16)
        try:
            for user in ('7', '8'):
                await service.handle_message(MessageEvent(user, f'private:{user}', None, user, user, '你好', (), False, (), 0))
            assert agent.calls == ['7']
            assert store.message_count('private:8') == 0
            assert agent.extractions == []
            await service.handle_message(MessageEvent('group', 'group:42', '42', '8', '8', '你好', (), True, (), 0))
            assert agent.calls == ['7', 'group']
        finally:
            await service.aclose()
            store.database.close()
    asyncio.run(scenario())


def test_runtime_wires_the_configured_persistent_quota(tmp_path):
    from qqbot.bootstrap.app import BotApp
    from qqbot.bootstrap.runtime import close_sync
    persona = tmp_path / 'persona.md'
    persona.write_text('测试人格')
    with patch.dict('os.environ', {
        'BOT_ONEBOT_TOKEN': 'test', 'BOT_MODEL_API_KEY': 'test',
        'BOT_DB_PATH': str(tmp_path / 'bot.sqlite3'), 'BOT_PERSONA_PATH': str(persona),
        'BOT_PRIVATE_ENABLED': 'true', 'BOT_PRIVATE_DAILY_USER_LIMIT': '2',
        'BOT_EXTENSIONS': '', 'BOT_SCHEDULES_PATH': str(tmp_path / 'schedules.json'),
    }, clear=True):
        app = BotApp(Config.from_env())
    try:
        quota = getattr(app.service, 'private_admission', None)
        assert isinstance(quota, PrivateChatQuota)
        assert quota.daily_limit == 2
    finally:
        close_sync(app._closers)


def test_admission_failure_refuses_private_model_work(tmp_path):
    from types import SimpleNamespace
    def broken(*args):
        raise OSError('quota unavailable')
    async def scenario():
        store = Store(tmp_path / 'bot.sqlite3')
        agent = FakeAgent()
        service = ConversationService(agent, store, store, store, FakeSender(),
            bot_policy(frozenset({'42'}), 8, 'Asia/Shanghai', False, private_enabled=True))
        service.private_admission = SimpleNamespace(admit=broken)
        try:
            await service.handle_message(MessageEvent('7', 'private:7', None, '7', '7', '你好', (), False, (), 0))
            assert agent.calls == []
            assert store.message_count('private:7') == 0
        finally:
            await service.aclose()
            store.database.close()
    asyncio.run(scenario())
