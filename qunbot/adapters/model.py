from __future__ import annotations

import httpx


class ModelClient:
    """Minimal OpenAI-compatible chat adapter. Provider-specific adapters can replace it."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url, self.api_key, self.model = base_url, api_key, model
        self.client = httpx.AsyncClient(timeout=90)

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.7,
    ) -> dict:
        if not self.api_key:
            raise RuntimeError("BOT_MODEL_API_KEY is not configured")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        response = await self.client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        await self.client.aclose()
