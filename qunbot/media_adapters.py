"""Filesystem meme catalog and optional OpenAI-compatible speech adapter."""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx


class LocalMemeCatalog:
    def __init__(self, root: Path):
        self.root = root.resolve()
        manifest = self.root / "catalog.json"
        raw = (
            json.loads(manifest.read_text(encoding="utf-8"))
            if manifest.exists()
            else {"memes": []}
        )
        self.entries: dict[str, list[Path]] = {}
        for item in raw.get("memes", []):
            tag = str(item["tag"])
            relative = Path(str(item["file"]))
            target = (self.root / relative).resolve()
            if not target.is_relative_to(self.root) or target.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".gif",
                ".webp",
            }:
                raise ValueError(f"invalid meme file path: {relative}")
            if target.is_file():
                self.entries.setdefault(tag, []).append(target)

    def pick(self, tag: str) -> str | None:
        files = self.entries.get(tag, [])
        if not files:
            return None
        data = random.choice(files).read_bytes()
        if len(data) > 4 * 1024 * 1024:
            return None
        return "base64://" + base64.b64encode(data).decode("ascii")

    def available_tags(self) -> list[str]:
        return sorted(self.entries)


class HttpSpeechSource:
    def __init__(self, base_url: str, api_key: str, model: str, voice: str):
        self.base_url, self.api_key, self.model, self.voice = (
            base_url,
            api_key,
            model,
            voice,
        )
        self.client = httpx.AsyncClient(timeout=60)

    async def synthesize(self, text: str) -> str:
        result = await self.client.post(
            f"{self.base_url}/audio/speech",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "voice": self.voice,
                "input": text,
                "response_format": "mp3",
            },
        )
        result.raise_for_status()
        if len(result.content) > 8 * 1024 * 1024:
            raise ValueError("speech output too large")
        return "base64://" + base64.b64encode(result.content).decode("ascii")

    async def close(self) -> None:
        await self.client.aclose()


class DashScopeSpeechSource:
    """Qwen-TTS non-streaming HTTP adapter; returns OneBot-ready audio bytes."""

    def __init__(self, base_url: str, api_key: str, model: str, voice: str):
        self.base_url, self.api_key, self.model, self.voice = (
            base_url.rstrip("/"),
            api_key,
            model,
            voice,
        )
        self.client = httpx.AsyncClient(timeout=60)

    async def synthesize(self, text: str) -> str:
        response = await self.client.post(
            f"{self.base_url}/services/aigc/multimodal-generation/generation",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "input": {
                    "text": text,
                    "voice": self.voice,
                    "language_type": "Chinese",
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        audio_url = ((payload.get("output") or {}).get("audio") or {}).get("url")
        if not isinstance(audio_url, str):
            raise TypeError("DashScope did not return an audio URL")
        parsed = urlsplit(audio_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not parsed.hostname.endswith(".aliyuncs.com")
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError("DashScope returned an unexpected audio URL")
        # DashScope may return an HTTP OSS signed URL. Require TLS for download.
        audio_url = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
        async with self.client.stream("GET", audio_url) as audio:
            audio.raise_for_status()
            chunks = []
            total = 0
            async for chunk in audio.aiter_bytes():
                total += len(chunk)
                if total > 8 * 1024 * 1024:
                    raise ValueError("speech output too large")
                chunks.append(chunk)
        return "base64://" + base64.b64encode(b"".join(chunks)).decode("ascii")

    async def close(self) -> None:
        await self.client.aclose()
