"""OpenAI-compatible, Qwen-TTS, and CosyVoice speech adapters."""

from __future__ import annotations

import base64
from html import escape
import re
from urllib.parse import urlsplit, urlunsplit

import httpx


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
        return await self._synthesize_body(self.request_body(text))

    async def _synthesize_body(self, body: dict) -> str:
        response = await self.client.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
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

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/services/aigc/multimodal-generation/generation"

    def request_body(self, text: str) -> dict:
        return {
            "model": self.model,
            "input": {"text": text, "voice": self.voice, "language_type": "Chinese"},
        }

    async def close(self) -> None:
        await self.client.aclose()


class CosyVoiceSpeechSource(DashScopeSpeechSource):
    """CosyVoice HTTP adapter with restrained SSML prosody, not fake emotion tags.

    Long Feifei supports SSML but not CosyVoice instruction control. The
    delivery styles only adjust pacing/pitch slightly; the actual words and
    punctuation still carry most of the expression.
    """

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/services/audio/tts/SpeechSynthesizer"

    @staticmethod
    def expressive_text(text: str, style: str = "neutral") -> str:
        # An explicit style is preferred. Legacy callers get a restrained cue
        # fallback; no generic question or sentence is auto-brightened.
        if style == "neutral":
            if any(cue in text for cue in ("难过", "别急", "辛苦", "先休息", "慢慢来")):
                style = "warm"
            elif any(cue in text for cue in ("哈哈", "好耶", "可以呀", "真棒")):
                style = "playful"
        rate, pitch = {
            "neutral": ("1", "1"),
            "warm": ("0.97", "0.99"),
            "playful": ("1.04", "1.02"),
            "excited": ("1.07", "1.03"),
            "serious": ("0.96", "0.98"),
        }.get(style, ("1", "1"))
        # Only an ellipsis gets an explicit breath-sized pause. Commas and
        # sentence stops are left to the TTS model's own prosody.
        spoken = re.sub(r"(?:…{1,2}|\.{3,})", '<break time="180ms"/>', escape(text))
        return f'<speak rate="{rate}" pitch="{pitch}">{spoken}</speak>'

    def request_body(self, text: str, style: str = "neutral") -> dict:
        return {
            "model": self.model,
            "input": {
                "text": self.expressive_text(text, style),
                "voice": self.voice,
                "format": "wav",
                "sample_rate": 24000,
                "enable_ssml": True,
            },
        }

    async def synthesize_styled(self, text: str, style: str = "neutral") -> str:
        return await self._synthesize_body(self.request_body(text, style))


class QwenAudioSpeechSource(DashScopeSpeechSource):
    """Qwen-Audio TTS: stable voice identity with restrained per-reply delivery."""

    STYLE_INSTRUCTIONS = {
        "neutral": "像在朋友群里自然接话，语气放松，避免播报腔和夸张表演。",
        "warm": "像熟悉的朋友轻声安慰，温柔但不刻意煽情，保留自然停顿。",
        "playful": "带一点笑意和轻松调侃，像朋友聊天，不要夸张卖萌。",
        "excited": "稍微开心、轻快一点，保持日常聊天的自然感，不要喊叫。",
        "serious": "语气认真、平稳，像朋友诚恳地说话，不要新闻播报腔。",
    }

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/services/audio/tts/SpeechSynthesizer"

    def request_body(self, text: str, style: str = "neutral") -> dict:
        return {
            "model": self.model,
            "input": {
                "text": text,
                "voice": self.voice,
                "format": "wav",
                "sample_rate": 24000,
                "instruction": self.STYLE_INSTRUCTIONS.get(style, self.STYLE_INSTRUCTIONS["neutral"]),
            },
        }

    async def synthesize_styled(self, text: str, style: str = "neutral") -> str:
        return await self._synthesize_body(self.request_body(text, style))
