from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import QQChannelConfig


class QQChannelError(RuntimeError):
    pass


class QQChannelClient:
    """Narrow adapter for QQ Open Platform; credentials never leave this edge."""

    def __init__(self, config: QQChannelConfig, *, transport=None, clock=time.time):
        self.config = config
        self._clock = clock
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            timeout=config.timeout_seconds, transport=transport, trust_env=False
        )

    @property
    def api_base(self) -> str:
        return "https://sandbox.api.sgroup.qq.com" if self.config.sandbox else "https://api.sgroup.qq.com"

    async def _access_token(self) -> str:
        if self._token and self._clock() < self._expires_at - 60:
            return self._token
        async with self._lock:
            if self._token and self._clock() < self._expires_at - 60:
                return self._token
            response = await self._http.post(
                "https://bots.qq.com/app/getAppAccessToken",
                json={"appId": self.config.app_id, "clientSecret": self.config.app_secret},
            )
            response.raise_for_status()
            data = response.json()
            token = str(data.get("access_token") or "")
            if not token:
                raise QQChannelError(f"QQ token request failed: {data.get('message') or 'unknown error'}")
            self._token = token
            self._expires_at = self._clock() + int(data.get("expires_in") or 600)
            return token

    async def request(
        self, method: str, path: str, *, query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        token = await self._access_token()
        response = await self._http.request(
            method, self.api_base + path,
            headers={"Authorization": f"QQBot {token}"}, params=query, json=body,
        )
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = {"message": response.text[:200]}
            raise QQChannelError(
                f"QQ channel API {response.status_code}: "
                f"{detail.get('code', '')} {detail.get('message', '')}".strip()
            )
        return response.json() if response.content else {}

    async def aclose(self) -> None:
        await self._http.aclose()
