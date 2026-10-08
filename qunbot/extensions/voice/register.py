"""Voice config and provider selection stay outside the core."""

import os

from .sources import CosyVoiceSpeechSource, DashScopeSpeechSource, HttpSpeechSource, QwenAudioSpeechSource


def _settings() -> tuple[str, str, str, str, str]:
    provider = os.getenv("BOT_TTS_PROVIDER", "openai").lower()
    if provider not in {"openai", "dashscope", "cosyvoice", "qwen_audio"}:
        raise ValueError("BOT_TTS_PROVIDER must be openai, dashscope, cosyvoice, or qwen_audio")
    values = (
        os.getenv("BOT_TTS_BASE_URL", "").rstrip("/"),
        os.getenv("BOT_TTS_API_KEY", ""),
        os.getenv("BOT_TTS_MODEL", ""),
        os.getenv("BOT_TTS_VOICE", ""),
    )
    if not all(values):
        raise ValueError("all BOT_TTS_* settings are required when voice is enabled")
    return provider, *values


def register(host, _config, _model) -> None:
    provider, url, key, model, voice = _settings()
    source = {
        "dashscope": DashScopeSpeechSource,
        "cosyvoice": CosyVoiceSpeechSource,
        "qwen_audio": QwenAudioSpeechSource,
        "openai": HttpSpeechSource,
    }[provider]
    speech = source(url, key, model, voice)
    host.speech_source = speech
    host.closers.append(speech.close)


def validate() -> dict:
    provider, *_ = _settings()
    return {"provider": provider}
