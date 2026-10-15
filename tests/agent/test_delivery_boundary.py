"""Actual outbound delivery has one owner independently of turn orchestration."""
import logging
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from qqbot.agent.conversation.delivery import ConversationDelivery
from qqbot.agent.service import ConversationService
from tests.support import bot_policy
from qqbot.actions.replies.models import PartReceipt, ReplyPlan
from tests.agent.test_service import FakeAgent, FakeSender, event
from tests.support import Store


class DeliveryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'bot.sqlite3')
        self.services = []

    async def asyncTearDown(self):
        for service in self.services:
            await service.aclose()
        self.store.db.close()
        self.tmp.cleanup()

    def new(self, **options):
        service = ConversationService(FakeAgent(), self.store, self.store, self.store,
            FakeSender(), bot_policy(frozenset({'42'}), 8, 'UTC', False),
            restore_last_reply=False, **options)
        self.services.append(service)
        self.assertIsInstance(getattr(service, 'delivery', None), ConversationDelivery)
        self.assertFalse(hasattr(service, '_deliver'), 'No duplicate delivery implementation')
        return service

    async def test_service_owns_one_delivery_component(self):
        service = self.new()
        self.assertIs(service.delivery.conversations, self.store)

    async def test_plain_record_writes_once_and_syncs_prompt_session(self):
        sessions = SimpleNamespace(append=lambda scope, rows: recorded.append((scope, rows)))
        recorded = []
        service = self.new(prompt_sessions=sessions)
        service.record_outbound(event('one'), ' 已发送 ', event_id='job:one')
        self.assertEqual(self.store.recent('group:42', 3)[0]['content'], ' 已发送 ')
        self.assertEqual(recorded, [('group:42', [{'role':'assistant', 'content':'已发送'}])])

    async def test_prompt_sync_failure_does_not_lose_delivery_ledger(self):
        def broken(*args):
            raise RuntimeError('prompt sync unavailable')
        service = self.new(prompt_sessions=SimpleNamespace(append=broken))
        with self.assertLogs('qqbot.agent.service', level='ERROR'):
            service.record_outbound(event('one'), 'sent', event_id='job:one')
        self.assertEqual(len(self.store.actual_recent('group:42')), 1)

    async def test_private_delivery_never_enters_group_intelligence(self):
        seen = []
        intelligence = SimpleNamespace(observe=seen.append)
        service = self.new(conversation_intelligence=intelligence)
        incoming = replace(event('p'), group_id=None, scope='private:7')
        service.record_outbound(incoming, 'sent', event_id='p:reply')
        self.assertEqual(seen, [])
        self.assertEqual(len(self.store.actual_recent('private:7')), 1)

    async def test_operator_record_links_latest_group_focus(self):
        seen = []
        intelligence = SimpleNamespace(observe=seen.append,
            latest_frame=lambda scope: SimpleNamespace(focus=SimpleNamespace(platform_message_id='focus')))
        service = self.new(conversation_intelligence=intelligence)
        service.record_outbound(replace(event('op'), origin='operator'), 'sent', event_id='op:reply')
        self.assertEqual(seen[0].reply_to_message_id, 'focus')
        self.assertEqual(seen[0].reply_to_user_id, '')
        self.assertEqual(seen[0].origin, 'assistant')

    async def test_part_receipt_records_only_success_and_keeps_metadata(self):
        class Dispatcher:
            supports_part_receipts = True
            async def dispatch(self, plan, **kwargs):
                await kwargs['on_intent'](0, 'text', 'part')
                await kwargs['on_receipt'](PartReceipt(0, 'text', 'part', 'qq-id', 'sent'))
                return SimpleNamespace(sent=True, transcript='part')
        service = self.new(reply_dispatcher=Dispatcher())
        incoming = event('parts')
        plan = ReplyPlan('part', ('text',), quote_message_id='quote')
        result = await service.delivery.deliver(plan, group_id='42', user_id=None,
                                               allowed_at=frozenset(), event=incoming)
        self.assertEqual(result.transcript, 'part')
        row = self.store.actual_recent('group:42')[0]
        self.assertEqual(row['platform_message_id'], 'qq-id')
        self.assertEqual(row['reply_to_id'], 'quote')
        service.record_outbound(incoming, 'part', event_id='job:parts')
        self.assertEqual(len(self.store.actual_recent('group:42')), 1)

    async def test_unknown_part_is_settled_without_assistant_message(self):
        class Dispatcher:
            supports_part_receipts = True
            async def dispatch(self, plan, **kwargs):
                await kwargs['on_intent'](0, 'text', 'maybe')
                await kwargs['on_receipt'](PartReceipt(0, 'text', 'maybe', '', 'unknown'))
                return SimpleNamespace(sent=False, transcript='')
        service = self.new(reply_dispatcher=Dispatcher())
        with self.assertLogs('qqbot.agent.service', level='WARNING'):
            await service.delivery.deliver(ReplyPlan('maybe', ('text',)), group_id='42', user_id=None,
                allowed_at=frozenset(), event=event('unknown'))
        self.assertEqual(len(self.store.actual_recent('group:42')), 0)

    async def test_delivery_without_trigger_does_not_install_receipt_callbacks(self):
        class Dispatcher:
            supports_part_receipts = True
            async def dispatch(self, plan, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(sent=True, transcript=plan.text)
        captured = {}
        service = self.new(reply_dispatcher=Dispatcher())
        self.assertEqual(await service.send_reply('42', None, 'hi'), 'hi')
        self.assertNotIn('on_receipt', captured)
        self.assertNotIn('on_intent', captured)

    async def test_observation_logger_is_injected_and_repository_has_explicit_port(self):
        from qqbot.agent.observation import ObservationDispatcher
        from qqbot.contracts import ports
        self.assertTrue(hasattr(ports, 'ObservationRepository'))
        logger = logging.getLogger('delivery.observation-test')
        dispatcher = ObservationDispatcher(backlog=1, logger=logger)
        try:
            async def observe(*args):
                raise RuntimeError('failed')
            observer = SimpleNamespace(observe=observe)
            dispatcher.submit('a', observer, event('a'), 'hi')
            with self.assertLogs(logger, level='WARNING'):
                self.assertFalse(dispatcher.submit('b', observer, event('b'), 'hi'))
            with self.assertLogs(logger, level='ERROR'):
                await dispatcher.aclose()
        finally:
            await dispatcher.aclose()

    async def test_repeated_outbound_record_does_not_repeat_prompt_or_projection(self):
        prompts, projected = [], []
        service = self.new(prompt_sessions=SimpleNamespace(append=lambda *args: prompts.append(args)),
            conversation_intelligence=SimpleNamespace(observe=projected.append))
        service.record_outbound(event('duplicate'), 'sent', event_id='job:duplicate')
        service.record_outbound(event('duplicate'), 'sent', event_id='job:duplicate')
        self.assertEqual(len(self.store.actual_recent('group:42')), 1)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(len(projected), 1)
