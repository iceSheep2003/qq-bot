"""OneBot adapter: recorded-frame replay, per-group ordering, backpressure.

A fake connection stands in for NapCat. No socket is opened, nothing is sent to
a real group, and no recorded frame leaves this module.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from qunbot.adapters.events import parse_message
from qunbot.adapters.onebot import (
    OneBotActionError,
    OneBotDisconnected,
    OneBotGateway,
    OneBotNotConnected,
    OneBotTimeout,
    SerialDispatcher,
    event_key,
)

_SENTINEL = object()


class FakeConnection:
    """Minimal stand-in for a websockets ServerConnection."""

    def __init__(self, *, failures: int = 0):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.failures = failures
        self.responded: tuple | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is _SENTINEL:
            raise StopAsyncIteration
        return item

    async def feed(self, frame: object) -> None:
        await self.queue.put(frame)

    async def close(self) -> None:
        await self.queue.put(_SENTINEL)

    async def send(self, data: str) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionResetError("link reset before the frame left")
        self.sent.append(data)

    def respond(self, status: int, text: str):
        self.responded = (status, text)
        return status


def frame(message_id: int, group: int, text: str = "你好") -> str:
    return json.dumps(
        {
            "post_type": "message",
            "message_type": "group",
            "message_id": message_id,
            "self_id": 99,
            "user_id": 7,
            "group_id": group,
            "sender": {"nickname": "小明"},
            "message": [{"type": "text", "data": {"text": text}}],
        },
        ensure_ascii=False,
    )


def response(echo: str, *, retcode: int = 0, data: dict | None = None) -> str:
    return json.dumps(
        {"status": "ok" if retcode == 0 else "failed", "retcode": retcode, "echo": echo,
         "data": data or {}},
        ensure_ascii=False,
    )


async def wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0)


class Request:
    def __init__(self, path: str, headers: dict | None = None):
        self.path = path
        self.headers = headers or {}


class OneBotTests(unittest.TestCase):
    def gateway(self, handler, **kwargs) -> OneBotGateway:
        gateway = OneBotGateway("127.0.0.1", 0, "secret", **kwargs)
        if handler is not None:
            gateway.on_event = handler
        return gateway

    def test_media_placeholder_cannot_be_sent_without_real_media(self):
        gateway = self.gateway(None)
        with self.assertRaisesRegex(ValueError, "media placeholder"):
            asyncio.run(gateway.send(group_id="42", text="[图片]"))

    def test_media_placeholder_is_dropped_when_real_image_exists(self):
        gateway = self.gateway(None)
        calls = []

        async def call(action, params, **_kwargs):
            calls.append((action, params))
            return {}

        gateway.call = call
        asyncio.run(gateway.send(group_id="42", text="[图片]", image="https://img/1"))
        self.assertEqual(calls[0][1]["message"], [
            {"type": "image", "data": {"file": "https://img/1"}}
        ])

    # --- recorded inbound frames -----------------------------------------

    def test_recorded_group_frames_arrive_in_order(self):
        seen: list[str] = []

        async def handler(data):
            seen.append(parse_message(data).event_id)

        gateway = self.gateway(handler)
        connection = FakeConnection()

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            for index in range(3):
                await connection.feed(frame(index, 42))
            await connection.close()
            await task

        asyncio.run(run())
        self.assertEqual(seen, ["99:0", "99:1", "99:2"])

    def test_each_group_keeps_its_own_order_and_groups_overlap(self):
        """Same group is serial; two groups are not forced to wait on each other."""
        lanes: dict[str, list[str]] = {}
        active = 0
        peak = 0
        release = None

        async def handler(data):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            scope = event_key(data)
            lanes.setdefault(scope, []).append(str(data["message_id"]))
            try:
                await release.wait()
            finally:
                active -= 1

        gateway = self.gateway(handler)
        connection = FakeConnection()

        async def run():
            nonlocal release
            release = asyncio.Event()
            task = asyncio.create_task(gateway.handle(connection))
            for message_id, group in ((0, 42), (1, 43), (2, 42), (3, 43)):
                await connection.feed(frame(message_id, group))
            await wait_for(lambda: peak == 2)
            release.set()
            await wait_for(lambda: sum(len(v) for v in lanes.values()) == 4)
            await connection.close()
            await task

        asyncio.run(run())
        self.assertEqual(lanes["group:42"], ["0", "2"])
        self.assertEqual(lanes["group:43"], ["1", "3"])
        self.assertEqual(peak, 2, "different groups must be able to run together")

    def test_duplicate_frames_are_delivered_and_left_to_the_service(self):
        """The gateway is a transport: deduplication belongs to the service."""
        seen: list[str] = []

        async def handler(data):
            seen.append(parse_message(data).event_id)

        gateway = self.gateway(handler)
        connection = FakeConnection()

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            await connection.feed(frame(1, 42))
            await connection.feed(frame(1, 42))
            await connection.close()
            await task

        asyncio.run(run())
        self.assertEqual(seen, ["99:1", "99:1"])

    def test_a_private_frame_gets_its_own_lane(self):
        self.assertEqual(event_key({"group_id": 42}), "group:42")
        self.assertEqual(event_key({"user_id": 7}), "private:7")
        self.assertEqual(event_key({}), "other")

    def test_message_frames_that_cannot_be_parsed_are_not_dispatched(self):
        seen = []

        async def handler(data):
            seen.append(data)

        gateway = self.gateway(handler)
        connection = FakeConnection()

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            # A heartbeat and a group frame with no user id: delivered to the
            # adapter, rejected by the parser, so the service never sees them.
            await connection.feed(json.dumps({"post_type": "meta_event"}))
            await connection.feed(
                json.dumps({"post_type": "message", "message_type": "group", "message_id": 5})
            )
            await connection.feed(frame(6, 42))
            await connection.close()
            await task

        asyncio.run(run())
        parsed = [parse_message(item) for item in seen]
        self.assertEqual([p for p in parsed if p], [parse_message(json.loads(frame(6, 42)))])

    def test_malformed_frames_do_not_close_the_connection(self):
        seen = []

        async def handler(data):
            seen.append(data["message_id"])

        gateway = self.gateway(handler)
        connection = FakeConnection()

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            for bad in ("not json", "5", "[]", "null", b"\xff\xfe", ""):
                await connection.feed(bad)
            await connection.feed(frame(7, 42))
            await wait_for(lambda: seen == [7])
            # The connection survived every hostile frame.
            self.assertFalse(task.done())
            self.assertEqual(gateway.connection, connection)
            await connection.close()
            await task

        asyncio.run(run())
        self.assertEqual(seen, [7])

    # --- outbound calls and recorded responses ----------------------------

    def test_a_recorded_response_resolves_a_pending_call(self):
        gateway = self.gateway(lambda data: asyncio.sleep(0))
        connection = FakeConnection()
        result = {}

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            await wait_for(lambda: gateway.connection is connection)
            pending = asyncio.create_task(gateway.call("get_status", {}))
            await wait_for(lambda: connection.sent)
            echo = json.loads(connection.sent[0])["echo"]
            await connection.feed(response(echo, data={"online": True, "good": False}))
            result["value"] = await pending
            await connection.close()
            await task

        asyncio.run(run())
        self.assertEqual(result["value"], {"online": True, "good": False})

    def test_a_failed_retcode_raises_an_action_error(self):
        gateway = self.gateway(lambda data: asyncio.sleep(0))
        connection = FakeConnection()
        raised = {}

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            await wait_for(lambda: gateway.connection is connection)
            pending = asyncio.create_task(gateway.call("send_group_msg", {}))
            await wait_for(lambda: connection.sent)
            echo = json.loads(connection.sent[0])["echo"]
            await connection.feed(response(echo, retcode=100))
            try:
                await pending
            except OneBotActionError as error:
                raised["error"] = str(error)
            await connection.close()
            await task

        asyncio.run(run())
        self.assertIn("100", raised["error"])

    def test_a_send_timeout_is_not_retried(self):
        gateway = self.gateway(lambda data: asyncio.sleep(0), request_timeout=0.05)
        connection = FakeConnection()

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            await wait_for(lambda: gateway.connection is connection)
            try:
                await gateway.call("send_group_msg", {})
            except OneBotTimeout:
                pass
            await connection.close()
            await task

        asyncio.run(run())
        self.assertEqual(len(connection.sent), 1, "a timeout must not resend")
        self.assertEqual(gateway.pending, {})

    def test_a_retry_is_opt_in_and_replays_the_frame(self):
        gateway = self.gateway(lambda data: asyncio.sleep(0))
        connection = FakeConnection(failures=1)
        result = {}

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            await wait_for(lambda: gateway.connection is connection)
            pending = asyncio.create_task(gateway.call("get_status", {}, attempts=2))
            await wait_for(lambda: connection.sent)
            echo = json.loads(connection.sent[0])["echo"]
            await connection.feed(response(echo, data={"ok": True}))
            result["value"] = await pending
            await connection.close()
            await task

        asyncio.run(run())
        self.assertEqual(result["value"], {"ok": True})
        self.assertEqual(len(connection.sent), 1)

    def test_a_disconnect_fails_every_pending_call(self):
        gateway = self.gateway(lambda data: asyncio.sleep(0))
        connection = FakeConnection()
        raised = {}

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            await wait_for(lambda: gateway.connection is connection)
            pending = asyncio.create_task(gateway.call("get_status", {}))
            await wait_for(lambda: connection.sent)
            await connection.close()
            await task
            try:
                await pending
            except OneBotDisconnected as error:
                raised["error"] = str(error)

        asyncio.run(run())
        self.assertIn("disconnected", raised["error"])
        self.assertIsNone(gateway.connection)

    def test_calling_without_a_connection_fails_fast(self):
        gateway = self.gateway(lambda data: asyncio.sleep(0))
        with self.assertRaises(OneBotNotConnected):
            asyncio.run(gateway.call("get_status", {}))

    # --- backpressure and shutdown ---------------------------------------

    def test_the_inbound_backlog_is_bounded(self):
        entered = None
        release = None

        async def handler(data):
            entered.set()
            await release.wait()

        gateway = self.gateway(handler, inbound_backlog=1, max_lanes=4)
        connection = FakeConnection()

        async def run():
            nonlocal entered, release
            entered, release = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(gateway.handle(connection))
            await connection.feed(frame(0, 42))
            await wait_for(entered.is_set)
            for index in range(1, 10):
                await connection.feed(frame(index, 42))
            await asyncio.sleep(0.02)
            accepted, dropped = gateway.inbound.accepted, gateway.inbound.dropped
            release.set()
            await connection.close()
            await task
            return accepted, dropped

        with self.assertLogs("qunbot.adapters.onebot", level="WARNING"):
            accepted, dropped = asyncio.run(run())
        self.assertEqual(accepted + dropped, 10)
        self.assertLessEqual(accepted, 2, "one running + one queued at most")
        self.assertGreaterEqual(dropped, 8)

    def test_aclose_drains_work_already_in_a_lane(self):
        seen: list[int] = []

        async def handler(data):
            await asyncio.sleep(0.01)
            seen.append(data["message_id"])

        gateway = self.gateway(handler)
        connection = FakeConnection()

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            await connection.feed(frame(0, 42))
            await connection.feed(frame(1, 42))
            await wait_for(lambda: len(seen) == 1)
            await gateway.aclose()
            await connection.close()
            await task

        asyncio.run(run())
        self.assertEqual(seen, [0, 1])

    def test_aclose_survives_being_cancelled_itself(self):
        """App shutdown cancels the gateway; the lanes must still stop."""
        entered = None

        async def handler(data):
            entered.set()
            await asyncio.Event().wait()

        gateway = self.gateway(handler)
        connection = FakeConnection()
        outcome = {}

        async def run():
            nonlocal entered
            entered = asyncio.Event()
            task = asyncio.create_task(gateway.handle(connection))
            await connection.feed(frame(0, 42))
            await wait_for(entered.is_set)
            closer = asyncio.create_task(gateway.aclose())
            await asyncio.sleep(0)
            closer.cancel()
            try:
                await closer
            except asyncio.CancelledError:
                outcome["cancelled"] = True
            await connection.close()
            await task

        asyncio.run(run())
        self.assertTrue(outcome.get("cancelled"))
        self.assertIsNone(gateway.connection)

    def test_the_lane_budget_is_capped(self):
        entered = None
        release = None

        async def handler(data):
            entered.set()
            await release.wait()

        gateway = self.gateway(handler, inbound_backlog=4, max_lanes=2)
        connection = FakeConnection()

        async def run():
            nonlocal entered, release
            entered, release = asyncio.Event(), asyncio.Event()
            task = asyncio.create_task(gateway.handle(connection))
            for group in (42, 43):
                await connection.feed(frame(group, group))
            await wait_for(lambda: gateway.inbound.accepted == 2)
            # A third group has no lane budget left and is refused, not queued.
            await connection.feed(frame(99, 44))
            await asyncio.sleep(0.02)
            dropped = gateway.inbound.dropped
            release.set()
            await connection.close()
            await task
            return dropped

        dropped = asyncio.run(run())
        self.assertGreaterEqual(dropped, 1)

    # --- handshake --------------------------------------------------------

    def test_process_request_checks_path_and_token(self):
        gateway = self.gateway(None)
        connection = FakeConnection()

        async def run():
            self.assertEqual(
                await gateway.process_request(connection, Request("/nope")), 404
            )
            self.assertEqual(
                await gateway.process_request(connection, Request("/ws")), 401
            )
            self.assertIsNone(
                await gateway.process_request(
                    connection, Request("/ws", {"Authorization": "Bearer secret"})
                )
            )

        asyncio.run(run())


class SerialDispatcherTests(unittest.TestCase):
    """The lane primitive, without any OneBot framing."""

    def test_items_for_one_key_run_one_at_a_time(self):
        order: list[int] = []
        active = 0
        peak = 0
        release = asyncio.Event()

        async def handler(item):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                if item == 0:
                    await release.wait()
                order.append(item)
            finally:
                active -= 1

        dispatcher = SerialDispatcher(handler, backlog=8, max_keys=2)

        async def run():
            for item in (0, 1, 2):
                dispatcher.submit("group:42", item)
            await wait_for(lambda: active == 1)
            release.set()
            await dispatcher.aclose()

        asyncio.run(run())
        self.assertEqual(order, [0, 1, 2])
        self.assertEqual(peak, 1)

    def test_a_full_lane_drops_instead_of_growing(self):
        async def handler(item):
            await asyncio.sleep(0)

        dispatcher = SerialDispatcher(handler, backlog=2, max_keys=1)

        async def run():
            accepted = [dispatcher.submit("group:42", index) for index in range(6)]
            await dispatcher.aclose()
            return accepted

        with self.assertLogs("qunbot.adapters.onebot", level="WARNING"):
            accepted = asyncio.run(run())
        self.assertEqual(accepted.count(True), 2)
        self.assertEqual(dispatcher.stats.dropped, 4)

    def test_a_failing_item_does_not_stop_the_lane(self):
        seen: list[int] = []

        async def handler(item):
            if item == 0:
                raise RuntimeError("boom")
            seen.append(item)

        dispatcher = SerialDispatcher(handler, backlog=8, max_keys=1)

        async def run():
            for item in (0, 1):
                dispatcher.submit("group:42", item)
            await dispatcher.aclose()

        with self.assertLogs("qunbot.adapters.onebot", level="ERROR"):
            asyncio.run(run())
        self.assertEqual(seen, [1])
        self.assertEqual(dispatcher.stats.failed, 1)


if __name__ == "__main__":
    unittest.main()
