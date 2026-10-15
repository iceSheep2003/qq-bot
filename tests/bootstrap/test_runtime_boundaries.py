"""Deferred background work and platform event routing have explicit owners."""
import asyncio
import importlib
import importlib.util
import logging
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from tests.agent.test_service import event


def boundary(test, module, name):
    test.assertIsNotNone(importlib.util.find_spec(module), 'Missing explicit runtime boundary')
    value = getattr(importlib.import_module(module), name, None)
    test.assertIsNotNone(value)
    return value


class RuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_worker_is_cancelled_before_resource_close(self):
        cls = boundary(self, 'qqbot.bootstrap.runtime', 'ApplicationRuntime')
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def sibling():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def broken():
            await started.wait()
            raise RuntimeError('worker broke')
        closed = []
        runtime = cls([sibling, broken], [lambda: closed.append(cancelled.is_set())])
        with self.assertRaisesRegex(RuntimeError, 'worker broke'):
            await runtime.run()
        self.assertEqual(closed, [True])

    async def test_caller_cancellation_stops_all_work_then_closes_in_reverse(self):
        cls = boundary(self, 'qqbot.bootstrap.runtime', 'ApplicationRuntime')
        started, stopped = asyncio.Event(), asyncio.Event()
        async def worker():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        order = []
        async def last():
            order.append(('last', stopped.is_set()))
        runtime = cls([worker], [lambda: order.append(('first', stopped.is_set())), last])
        running = asyncio.create_task(runtime.run())
        await started.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(order, [('last', True), ('first', True)])

    async def test_synchronous_factory_failure_still_closes_and_settles_started_work(self):
        cls = boundary(self, 'qqbot.bootstrap.runtime', 'ApplicationRuntime')
        def broken():
            raise ValueError('factory broke')
        closed = []
        runtime = cls([broken], [lambda: closed.append(True)])
        with self.assertRaisesRegex(ValueError, 'factory broke'):
            await runtime.run()
        self.assertEqual(closed, [True])

    async def test_normal_completion_closes_resources(self):
        cls = boundary(self, 'qqbot.bootstrap.runtime', 'ApplicationRuntime')
        work, closed = [], []
        async def worker():
            work.append('ran')
        runtime = cls([worker], [lambda: closed.append('closed')])
        self.assertEqual(work, [])
        await runtime.run()
        self.assertEqual(work, ['ran'])
        self.assertEqual(closed, ['closed'])

    async def test_background_feature_factory_is_not_called_until_worker_runs(self):
        from qqbot.features import loader
        from qqbot.features.features import FeatureHost
        manifest = SimpleNamespace(name='fixture')
        called = []
        async def background():
            called.append('run')
        def factory(*args):
            called.append('created')
            return background()
        with mock.patch.object(loader, 'resolve_plan', return_value=SimpleNamespace(background=(manifest,))), mock.patch.object(loader, '_entry_callable', return_value=factory):
            workers = loader.build_workers(SimpleNamespace(), object(), object(), FeatureHost())
        try:
            self.assertEqual(called, [])
            self.assertTrue(callable(workers[0]))
            await workers[0]()
            self.assertEqual(called, ['created', 'run'])
        finally:
            for worker in workers:
                if asyncio.iscoroutine(worker):
                    worker.close()


class EventRoutingTests(unittest.IsolatedAsyncioTestCase):
    def router(self, incoming=None, *, handlers=(), consumed=False, private=False, quote=None, fail=False):
        cls = boundary(self, 'qqbot.interfaces.qq.router', 'QqEventRouter')
        seen, quoted = [], []
        async def handle(value):
            if fail:
                raise RuntimeError('handler failed')
            seen.append(value)
        async def poke(raw):
            return consumed
        async def resolve(identity, **kwargs):
            quoted.append(identity)
            if isinstance(quote, Exception):
                raise quote
            return quote or {'text':'quoted', 'image_urls':['image']}
        instance = cls(service=SimpleNamespace(handle_message=handle),
            gateway=SimpleNamespace(resolve_quote=resolve),
            allowed_groups=frozenset({'42'}), private_enabled=private,
            poke_controller=SimpleNamespace(handle=poke), inbound_handlers=handlers,
            parse_event=lambda raw: incoming, logger=logging.getLogger('routing.acceptance'))
        return instance, seen, quoted

    async def test_group_and_private_admission_precedes_quote_resolution(self):
        for incoming in (event('blocked', group='99'), replace(event('private'), group_id=None, scope='private:7')):
            router, seen, quoted = self.router(replace(incoming, reply_to_message_id='secret'))
            await router({})
            self.assertEqual(seen, [])
            self.assertEqual(quoted, [])

    async def test_enabled_private_preserves_private_scope(self):
        router, seen, _ = self.router(replace(event('p'), group_id=None, scope='private:7'), private=True)
        await router({})
        self.assertEqual(seen[0].scope, 'private:7')

    async def test_quote_is_enriched_before_conversation(self):
        router, seen, quoted = self.router(replace(event('q'), reply_to_message_id='source'))
        await router({})
        self.assertEqual(quoted, ['source'])
        self.assertEqual(seen[0].quoted_text, 'quoted')
        self.assertEqual(seen[0].quoted_image_urls, ('image',))

    async def test_failed_quote_is_advisory(self):
        router, seen, _ = self.router(replace(event('q'), reply_to_message_id='source'), quote=TimeoutError())
        with self.assertLogs('routing.acceptance', level='INFO'):
            await router({})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].quoted_text, '')

    async def test_consumed_poke_or_feature_never_reaches_conversation(self):
        router, seen, _ = self.router(event('p'), consumed=True)
        await router({})
        self.assertEqual(seen, [])
        async def consume(raw):
            return True
        router, seen, _ = self.router(event('f'), handlers=(consume,))
        await router({})
        self.assertEqual(seen, [])

    async def test_failed_feature_is_isolated_and_message_still_runs(self):
        async def broken(raw):
            raise RuntimeError('feature failed')
        router, seen, _ = self.router(event('f'), handlers=(broken,))
        with self.assertLogs('routing.acceptance', level='ERROR'):
            await router({})
        self.assertEqual(len(seen), 1)

    async def test_nonmessage_is_ignored(self):
        router, seen, _ = self.router()
        await router({})
        self.assertEqual(seen, [])

    async def test_conversation_failure_is_isolated_at_gateway_boundary(self):
        router, seen, _ = self.router(event('f'), fail=True)
        with self.assertLogs('routing.acceptance', level='ERROR'):
            await router({})
        self.assertEqual(seen, [])
