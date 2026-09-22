"""Optional embedding providers for hybrid retrieval.

The vector path is opt-in through the environment:

    BOT_MEMORY_EMBEDDING_PROVIDER = "" (default) | openai | hash
    BOT_MEMORY_EMBEDDING_BASE_URL   defaults to BOT_MODEL_BASE_URL
    BOT_MEMORY_EMBEDDING_API_KEY    defaults to BOT_MODEL_API_KEY
    BOT_MEMORY_EMBEDDING_MODEL      required for the openai provider
    BOT_MEMORY_EMBEDDING_DIM        optional, hash provider width

When nothing is configured ``embeddings_from_env()`` returns ``None`` and no
vector code runs at all. Every provider is defensive: a failure returns ``None``
and the caller falls back to lexical search. Nothing here may raise at import
or construction time — a broken endpoint must not stop the bot from starting.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Protocol

log = logging.getLogger(__name__)

DEFAULT_HASH_DIM = 256


class EmbeddingProvider(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]] | None: ...


class HashingEmbedder:
    """Deterministic local embedder: hashed character bigrams.

    Not semantic, but it needs no network, so it exercises the vector path in
    tests and lets a deployment run hybrid retrieval offline.
    """

    def __init__(self, dim: int = DEFAULT_HASH_DIM):
        self.dim = max(32, int(dim))
        self.name = f"hash-{self.dim}"

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        from .text import char_ngrams

        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            grams = char_ngrams(text) or {text}
            for gram in grams:
                digest = hashlib.sha1(gram.encode("utf-8")).digest()
                bucket = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] % 2 else -1.0
                vector[bucket] += sign
            vectors.append(vector)
        return vectors


class OpenAICompatibleEmbedder:
    """``POST {base_url}/embeddings`` — the same shape as the chat adapter."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 5.0):
        import httpx

        self.name = model
        self._model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def embed(self, texts: list[str]) -> list[list[float]] | None:
        try:
            response = self._client.post(
                "/embeddings", json={"model": self._model, "input": list(texts)}
            )
            response.raise_for_status()
            payload = response.json()
            data = sorted(payload["data"], key=lambda item: item.get("index", 0))
            return [list(item["embedding"]) for item in data]
        except Exception as exc:  # network, auth, schema — all mean "no vectors"
            log.warning("embedding request failed, falling back to lexical: %s", exc)
            return None

    def close(self) -> None:
        self._client.close()


def embeddings_from_env(env: dict | None = None) -> EmbeddingProvider | None:
    """Build the configured provider, or ``None`` when the vector path is off."""
    env = os.environ if env is None else env
    provider = (env.get("BOT_MEMORY_EMBEDDING_PROVIDER") or "").strip().lower()
    if not provider:
        return None
    try:
        if provider == "hash":
            return HashingEmbedder(int(env.get("BOT_MEMORY_EMBEDDING_DIM", DEFAULT_HASH_DIM)))
        if provider in ("openai", "openai-compatible"):
            model = (env.get("BOT_MEMORY_EMBEDDING_MODEL") or "").strip()
            if not model:
                log.warning(
                    "BOT_MEMORY_EMBEDDING_MODEL is required for provider=%s; "
                    "vector path disabled",
                    provider,
                )
                return None
            base_url = (
                env.get("BOT_MEMORY_EMBEDDING_BASE_URL")
                or env.get("BOT_MODEL_BASE_URL")
                or ""
            ).strip()
            api_key = (
                env.get("BOT_MEMORY_EMBEDDING_API_KEY")
                or env.get("BOT_MODEL_API_KEY")
                or ""
            )
            if not base_url:
                log.warning("no embedding base url configured; vector path disabled")
                return None
            return OpenAICompatibleEmbedder(base_url, api_key, model)
        log.warning("unknown BOT_MEMORY_EMBEDDING_PROVIDER=%s; vector path disabled", provider)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("embedding provider unavailable (%s); vector path disabled", exc)
    return None
