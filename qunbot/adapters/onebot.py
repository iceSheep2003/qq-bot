"""OneBot/NapCat reverse WebSocket adapter.

Inbound frames are handed to a bounded, per-lane serial executor instead of one
``create_task`` per frame. A reconnect, a burst or a malformed frame therefore
cannot spawn unbounded background tasks: each lane has a fixed backlog, the
number of lanes is capped, and an overflowing frame is counted and dropped.

Outbound calls have one explicit policy: **send once**. A retried send can post
the same group message twice, so retries are opt-in per call (``attempts=``) and
only the connection-level failures are classified as retryable. Timeouts and
disconnects surface as typed errors instead of being swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass

from websockets.asyncio.server import ServerConnection, serve

log = logging.getLogger(__name__)

# Frames are capped so a hostile peer cannot make the process allocate without
# bound; OneBot messages are text and images are passed by URL, not inline.
DEFAULT_MAX_FRAME_BYTES = 1 << 20
DEFAULT_REQUEST_TIMEOUT = 20.0
DEFAULT_INBOUND_BACKLOG = 64
DEFAULT_MAX_LANES = 32


class OneBotError(RuntimeError):
    """Base class for outbound OneBot failures."""


class OneBotNotConnected(OneBotError):
    """No NapCat connection is currently registered."""


class OneBotTimeout(OneBotError):
    """NapCat accepted the frame but did not answer in time."""


class OneBotDisconnected(OneBotError, ConnectionError):
    """The connection dropped while a request was in flight."""


class OneBotActionError(OneBotError):
    """NapCat answered, but reported a failure (non-zero retcode)."""


# Only these mean "the request never reached NapCat", so re-sending them cannot
# duplicate a group message. OneBotActionError is deliberately absent.
RETRYABLE = (OneBotNotConnected, OneBotTimeout, OneBotDisconnected)


def _clamp(value: int, *, minimum: int = 1, maximum: int = 1_000_000) -> int:
    return max(minimum, min(maximum, value))


def event_key(data: dict) -> str:
    """Ordering key for an inbound frame.

    One serial lane per group (and per private peer); anything without an
    identity shares a single lane so unknown traffic still cannot fan out.
    """
    if data.get("group_id") is not None:
        return f"group:{data['group_id']}"
    if data.get("user_id") is not None:
        return f"private:{data['user_id']}"
    return "other"


@dataclass
class InboundStats:
    accepted: int = 0
    dropped: int = 0
    completed: int = 0
    failed: int = 0


class SerialDispatcher:
    """Per-key serial, cross-key concurrent, bounded backlog.

    Same key: strictly FIFO, one handler invocation at a time. Different keys:
    concurrent, capped by ``max_keys``. A saturated lane drops and counts the
    item rather than growing memory or creating tasks. An idle lane releases its
    worker and its slot after ``idle_timeout`` seconds, so a burst of one-off
    keys cannot permanently exhaust the lane budget.
    """

    def __init__(
        self,
        handler: Callable[[object], Awaitable[None]],
        *,
        backlog: int,
        max_keys: int,
        idle_timeout: float = 60.0,
    ):
        self._handler = handler
        self._backlog = max(1, backlog)
        self._max_keys = max(1, max_keys)
        self._idle_timeout = idle_timeout
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._closed = False
        self.stats = InboundStats()

    @property
    def lanes(self) -> int:
        return len(self._queues)

    def submit(self, key: str, item: object) -> bool:
        """Enqueue an item for its lane. Returns False when it was refused."""
        if self._closed:
            return False
        queue = self._queues.get(key)
        worker = self._workers.get(key)
        if queue is None or (worker is not None and worker.done()):
            if queue is None and len(self._queues) >= self._max_keys:
                self.stats.dropped += 1
                log.warning("Inbound lane limit reached; dropping frame for %s", key)
                return False
            queue = asyncio.Queue(maxsize=self._backlog)
            self._queues[key] = queue
            self._workers[key] = asyncio.get_running_loop().create_task(
                self._run(key, queue)
            )
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            self.stats.dropped += 1
            log.warning("Inbound backlog full for %s; dropping frame", key)
            return False
        self.stats.accepted += 1
        return True

    async def _run(self, key: str, queue: asyncio.Queue) -> None:
        try:
            while True:
                # After close(), keep draining what is already queued.
                if self._closed and queue.empty():
                    break
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=self._idle_timeout
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    break
                try:
                    await self._handler(item)
                    self.stats.completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.stats.failed += 1
                    log.exception("Inbound dispatch failed for %s", key)
                finally:
                    queue.task_done()
        finally:
            if self._queues.get(key) is queue:
                self._queues.pop(key, None)
                self._workers.pop(key, None)

    async def aclose(self, timeout: float = 5.0) -> None:
        """Drain queued work, then cancel whatever is still running."""
        self._closed = True
        workers = list(self._workers.values())
        if not workers:
            return
        queues = list(self._queues.values())
        try:
            await asyncio.wait_for(
                asyncio.gather(*(q.join() for q in queues), return_exceptions=True),
                timeout=timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            log.warning("Inbound lanes did not drain within %.1fs", timeout)
        except asyncio.CancelledError:
            # Shutdown itself was cancelled: stop the lanes without waiting.
            for worker in workers:
                worker.cancel()
            raise
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._queues.clear()
        self._workers.clear()


class OneBotGateway:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        inbound_backlog: int | None = None,
        max_lanes: int | None = None,
        idle_lane_timeout: float = 60.0,
        request_timeout: float | None = None,
        max_frame_bytes: int | None = None,
    ):
        self.host, self.port, self.token = host, port, token
        self.connection: ServerConnection | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.on_event: Callable[[dict], Awaitable[None]] | None = None
        # Defaults live here; environment parsing lives in config.Config, which
        # app.py passes down. The adapter never reads os.environ.
        self.request_timeout = max(1.0, request_timeout or DEFAULT_REQUEST_TIMEOUT)
        self.max_frame_bytes = _clamp(
            max_frame_bytes or DEFAULT_MAX_FRAME_BYTES, maximum=1 << 24
        )
        self._dispatcher = SerialDispatcher(
            self._dispatch,
            backlog=_clamp(inbound_backlog or DEFAULT_INBOUND_BACKLOG, maximum=100_000),
            max_keys=_clamp(max_lanes or DEFAULT_MAX_LANES, maximum=10_000),
            idle_timeout=idle_lane_timeout,
        )
        self.inbound = self._dispatcher.stats

    # --- inbound ---------------------------------------------------------

    async def process_request(self, connection: ServerConnection, request):
        if request.path != "/ws":
            return connection.respond(404, "Not Found\n")
        if (
            self.token
            and request.headers.get("Authorization") != f"Bearer {self.token}"
        ):
            return connection.respond(401, "Unauthorized\n")
        return None

    async def run(self) -> None:
        try:
            async with serve(
                self.handle,
                self.host,
                self.port,
                process_request=self.process_request,
                ping_interval=20,
                max_size=self.max_frame_bytes,
            ):
                log.info(
                    "OneBot reverse WebSocket listening at ws://%s:%d/ws",
                    self.host,
                    self.port,
                )
                await asyncio.Future()
        finally:
            await self.aclose()

    async def handle(self, connection: ServerConnection) -> None:
        self.connection = connection
        try:
            async for raw in connection:
                data = self._decode(raw)
                if data is None:
                    continue
                if self._resolve_response(data):
                    continue
                self._dispatcher.submit(event_key(data), data)
        finally:
            if self.connection is connection:
                self.connection = None
            self._fail_pending(OneBotDisconnected("OneBot disconnected"))

    @staticmethod
    def _decode(raw: object) -> dict | None:
        """Parse one frame, or None. A bad frame is dropped, never fatal."""
        if not isinstance(raw, (str, bytes, bytearray)):
            log.warning("Ignoring non-text OneBot frame (%s)", type(raw).__name__)
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            log.warning("Ignoring malformed OneBot frame: %.200r", raw)
            return None
        if not isinstance(data, dict):
            log.warning("Ignoring non-object OneBot frame: %.200r", data)
            return None
        return data

    def _resolve_response(self, data: dict) -> bool:
        echo = str(data.get("echo") or "")
        if not echo:
            return False
        future = self.pending.pop(echo, None)
        if future is None:
            return False
        if not future.done():
            future.set_result(data)
        return True

    def _fail_pending(self, error: Exception) -> None:
        for future in self.pending.values():
            if not future.done():
                future.set_exception(error)
        self.pending.clear()

    async def _dispatch(self, data: dict) -> None:
        if self.on_event is not None:
            await self.on_event(data)

    async def aclose(self, timeout: float = 5.0) -> None:
        await self._dispatcher.aclose(timeout=timeout)

    # --- outbound --------------------------------------------------------

    async def call(self, action: str, params: dict, *, attempts: int = 1) -> dict:
        """Call a OneBot action.

        One attempt by default: re-sending can duplicate a group message, so
        retries are opt-in and only connection-level failures are retried.
        """
        attempts = max(1, attempts)
        for attempt in range(attempts):
            try:
                return await self._call_once(action, params)
            except RETRYABLE as error:
                if attempt + 1 >= attempts:
                    raise
                log.warning(
                    "OneBot %s attempt %d/%d failed: %s",
                    action,
                    attempt + 1,
                    attempts,
                    error,
                )
        raise OneBotError(f"OneBot {action} exhausted {attempts} attempts")

    async def _call_once(self, action: str, params: dict) -> dict:
        connection = self.connection
        if connection is None:
            raise OneBotNotConnected("NapCat is not connected")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            try:
                await connection.send(
                    json.dumps(
                        {"action": action, "params": params, "echo": echo},
                        ensure_ascii=False,
                    )
                )
            except Exception as error:
                # Nothing reached the wire, so a retry cannot duplicate a frame.
                raise OneBotDisconnected(f"OneBot send failed: {error}") from error
            try:
                response = await asyncio.wait_for(future, timeout=self.request_timeout)
            except (TimeoutError, asyncio.TimeoutError) as error:
                raise OneBotTimeout(
                    f"OneBot {action} timed out after {self.request_timeout}s"
                ) from error
            if response.get("status") != "ok" or response.get("retcode", 0) != 0:
                raise OneBotActionError(
                    f"OneBot {action} failed: "
                    f"{response.get('message', response.get('retcode'))}"
                )
            return response.get("data") or {}
        finally:
            self.pending.pop(echo, None)

    async def send(
        self,
        *,
        group_id: str | None = None,
        user_id: str | None = None,
        text: str = "",
        at_user: str | None = None,
        image: str | None = None,
        voice: str | None = None,
        allowed_at: Collection[str] | None = None,
    ) -> dict:
        if bool(group_id) == bool(user_id):
            raise ValueError("exactly one of group_id or user_id is required")
        try:
            target = int(group_id or user_id)
            at_qq = int(at_user) if at_user else None
        except (TypeError, ValueError) as error:
            raise OneBotError(
                f"non-numeric OneBot target: {group_id or user_id!r}"
            ) from error
        # ``at_user`` is a structured field, never a free-form OneBot command:
        # by the time it reaches here it is a bare QQ number. When the caller
        # supplies the group roster, an out-of-roster target is dropped rather
        # than turning the whole reply into a failure.
        if at_qq is not None and allowed_at is not None:
            if str(at_qq) not in {str(member) for member in allowed_at}:
                log.warning("Dropping @ target %s: not a known group member", at_qq)
                at_qq = None
        message = []
        if at_qq is not None and group_id:
            message.append({"type": "at", "data": {"qq": str(at_qq)}})
        if text:
            message.append({"type": "text", "data": {"text": text[:3000]}})
        if image:
            message.append({"type": "image", "data": {"file": image}})
        if voice:
            message.append({"type": "record", "data": {"file": voice}})
        if not message:
            raise ValueError("empty message")
        return await self.call(
            "send_group_msg" if group_id else "send_private_msg",
            {
                "group_id" if group_id else "user_id": target,
                "message": message,
            },
        )
