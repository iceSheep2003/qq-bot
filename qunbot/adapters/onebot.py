from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from websockets.asyncio.server import ServerConnection, serve

log = logging.getLogger(__name__)


class OneBotGateway:
    def __init__(self, host: str, port: int, token: str):
        self.host, self.port, self.token = host, port, token
        self.connection: ServerConnection | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.on_event: Callable[[dict], Awaitable[None]] | None = None

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
        async with serve(
            self.handle,
            self.host,
            self.port,
            process_request=self.process_request,
            ping_interval=20,
        ):
            log.info(
                "OneBot reverse WebSocket listening at ws://%s:%d/ws",
                self.host,
                self.port,
            )
            await asyncio.Future()

    async def handle(self, connection: ServerConnection) -> None:
        self.connection = connection
        try:
            async for raw in connection:
                try:
                    data = json.loads(raw)
                    echo = str(data.get("echo", ""))
                    if echo and echo in self.pending:
                        future = self.pending.pop(echo)
                        if not future.done():
                            future.set_result(data)
                    elif self.on_event:
                        asyncio.create_task(self.on_event(data))
                except (ValueError, TypeError):
                    log.exception("Invalid OneBot frame")
        finally:
            if self.connection is connection:
                self.connection = None
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("OneBot disconnected"))
            self.pending.clear()

    async def call(self, action: str, params: dict) -> dict:
        if not self.connection:
            raise ConnectionError("NapCat is not connected")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.connection.send(
                json.dumps(
                    {"action": action, "params": params, "echo": echo},
                    ensure_ascii=False,
                )
            )
            response = await asyncio.wait_for(future, timeout=20)
            if response.get("status") != "ok" or response.get("retcode", 0) != 0:
                raise RuntimeError(
                    f"OneBot {action} failed: {response.get('message', response.get('retcode'))}"
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
    ) -> dict:
        if bool(group_id) == bool(user_id):
            raise ValueError("exactly one of group_id or user_id is required")
        message = []
        if at_user and group_id:
            message.append({"type": "at", "data": {"qq": str(at_user)}})
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
                "group_id" if group_id else "user_id": int(group_id or user_id),
                "message": message,
            },
        )
