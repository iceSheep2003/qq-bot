from __future__ import annotations

import json
import re

from ...domain import MessageEvent
from .client import QQChannelClient
from .config import QQChannelConfig


_READ_PATHS = (
    re.compile(r"^/users/@me/guilds$"),
    re.compile(r"^/guilds/\d+/api_permission$"),
    re.compile(r"^/guilds/\d+/channels$"),
    re.compile(r"^/channels/\d+$"),
    re.compile(r"^/guilds/\d+/members(?:/\d+)?$"),
    re.compile(r"^/guilds/\d+/roles/\d+/members$"),
    re.compile(r"^/channels/\d+/online_nums$"),
    re.compile(r"^/channels/\d+/threads(?:/\d+)?$"),
)

_WRITE_PATHS = {
    "PUT": (re.compile(r"^/channels/\d+/threads$"),),
    "POST": (
        re.compile(r"^/channels/\d+/threads/\d+/comment$"),
        re.compile(r"^/guilds/\d+/announces$"),
        re.compile(r"^/channels/\d+/schedules$"),
    ),
    "PATCH": (re.compile(r"^/channels/\d+/schedules/\d+$"),),
}


def _clean_query(value) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("query must be an object")
    if len(value) > 10:
        raise ValueError("too many query parameters")
    return {str(k)[:50]: str(v)[:200] for k, v in value.items()}


class QQChannelService:
    def __init__(self, config: QQChannelConfig, client: QQChannelClient):
        self.config = config
        self.client = client

    async def query(self, args: dict, _event: MessageEvent) -> str:
        path = str(args.get("path") or "")
        if not any(pattern.fullmatch(path) for pattern in _READ_PATHS):
            raise ValueError("path is not in the QQ channel read allowlist")
        data = await self.client.request("GET", path, query=_clean_query(args.get("query")))
        return json.dumps(data, ensure_ascii=False)[:16000]

    async def write(self, args: dict, event: MessageEvent) -> str:
        if event.user_id not in self.config.operator_ids:
            raise ValueError("only a locally configured QQ channel operator may write")
        method = str(args.get("method") or "").upper()
        path = str(args.get("path") or "")
        patterns = _WRITE_PATHS.get(method, ())
        if not any(pattern.fullmatch(path) for pattern in patterns):
            raise ValueError("operation is not in the QQ channel write allowlist")
        body = args.get("body")
        if not isinstance(body, dict) or not body:
            raise ValueError("body must be a non-empty object")
        encoded = json.dumps(body, ensure_ascii=False)
        if len(encoded) > 8000:
            raise ValueError("body is too large")
        data = await self.client.request(method, path, body=body)
        return json.dumps(data, ensure_ascii=False)[:16000]
