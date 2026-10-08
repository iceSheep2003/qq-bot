"""OpenAI-compatible chat adapter with capability, retry and cache telemetry.

This adapter owns the *provider edge*: the HTTP call, the error taxonomy, the
retry/backoff policy, an optional circuit breaker and a rolling call budget.
Everything above it (``runtime.agent``, memory and relationship evaluators)
still only sees the OpenAI-compatible ``{"choices": [...], "usage": {...}}``
shape returned by :meth:`ModelClient.complete`.

Trust boundary
--------------
The API key and the content of prompts/replies are sensitive. They must never
reach logs, exception messages, telemetry or test output. This module only
records: HTTP status, error kind, attempt counts, provider-reported token
numbers and content *hashes*. :func:`redact` is provided for callers that need
to sanitise text before logging it.

Prompt-cache honesty
--------------------
A stable prefix hash proves the *request* prefix bytes did not change. It does
**not** prove the provider served a cached prefix. Only the ``cached_tokens``
number reported by the provider counts; when the field is absent the metric is
:data:`UNKNOWN`, never ``0``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, fields
from enum import Enum

import httpx

from ..domain import normalize_reasoning_effort

log = logging.getLogger(__name__)

UNKNOWN = "unknown"
"""Sentinel for a metric the provider did not report. Never coerce it to 0."""

_CACHE_SOURCES: tuple[tuple[str, ...], ...] = (
    ("prompt_tokens_details", "cached_tokens"),  # OpenAI / xAI style
    ("prompt_cache_hit_tokens",),  # DeepSeek style
    ("cache_read_input_tokens",),  # Anthropic style
    ("cached_tokens",),  # already-normalised / flat providers
)


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #
class ErrorKind(str, Enum):
    """Why a model call failed. Drives retry, circuit and caller behaviour."""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    BUDGET = "budget"
    CONFIG = "config"


class ModelError(RuntimeError):
    """Base for every adapter-level failure. Carries no prompt/summary content."""

    kind: ErrorKind = ErrorKind.PERMANENT
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        attempts: int = 1,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.attempts = attempts

    def with_attempts(self, attempts: int) -> "ModelError":
        self.attempts = attempts
        return self


class ModelTimeoutError(ModelError):
    kind = ErrorKind.TIMEOUT
    retryable = True


class ModelRateLimitError(ModelError):
    kind = ErrorKind.RATE_LIMIT
    retryable = True


class ModelTransientError(ModelError):
    kind = ErrorKind.TRANSIENT
    retryable = True


class ModelPermanentError(ModelError):
    kind = ErrorKind.PERMANENT
    retryable = False


class ModelBudgetExceeded(ModelError):
    kind = ErrorKind.BUDGET
    retryable = False


class ModelCircuitOpenError(ModelError):
    """Fail-fast while the breaker is open; retrying immediately is pointless."""

    kind = ErrorKind.TRANSIENT
    retryable = False


class ModelConfigError(ModelError):
    kind = ErrorKind.CONFIG
    retryable = False


def classify_status(status: int) -> ErrorKind:
    """Map an HTTP status to an :class:`ErrorKind`.

    408 timeout, 429 rate limit, 5xx transient (retry), everything else
    permanent (auth failures, bad request, unknown model, ...).
    """
    if status == 408:
        return ErrorKind.TIMEOUT
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if 500 <= status <= 599:
        return ErrorKind.TRANSIENT
    return ErrorKind.PERMANENT


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


def _error_for(status: int, retry_after: float | None) -> ModelError:
    kind = classify_status(status)
    message = f"model provider returned HTTP {status} ({kind.value})"
    if kind is ErrorKind.TIMEOUT:
        return ModelTimeoutError(message, status=status, retry_after=retry_after)
    if kind is ErrorKind.RATE_LIMIT:
        return ModelRateLimitError(message, status=status, retry_after=retry_after)
    if kind is ErrorKind.TRANSIENT:
        return ModelTransientError(message, status=status, retry_after=retry_after)
    return ModelPermanentError(message, status=status, retry_after=retry_after)


# --------------------------------------------------------------------------- #
# Secret redaction
# --------------------------------------------------------------------------- #
def redact(text: str, *secrets: str, limit: int = 200) -> str:
    """Replace secrets with ``***`` and truncate so it is safe to log.

    Only ever used for diagnostic strings; message content never goes through
    the log path in the first place.
    """
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    if len(out) > limit:
        out = out[:limit] + "…"
    return out


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with optional jitter for retryable failures."""

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay < 0 or self.max_delay < 0 or not 0 <= self.jitter <= 1:
            raise ValueError("invalid retry delays")

    def base_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Delay before the *next* attempt, after `attempt` failed attempts."""
        if retry_after is not None:
            return min(max(retry_after, 0.0), self.max_delay)
        return min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))


@dataclass(frozen=True)
class CircuitPolicy:
    """Failure backoff: open after N consecutive failures, probe after a pause."""

    failure_threshold: int = 5
    reset_after: float = 30.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.reset_after < 0:
            raise ValueError("reset_after must be >= 0")


@dataclass(frozen=True)
class BudgetPolicy:
    """Rolling-window call budget. ``None`` means unlimited on that axis."""

    max_requests: int | None = None
    max_tokens: int | None = None
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_requests is not None and self.max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")

    @property
    def enabled(self) -> bool:
        return self.max_requests is not None or self.max_tokens is not None


@dataclass(frozen=True)
class ModelCapabilities:
    """What the configured model is known to support.

    ``None`` means *unverified* rather than unsupported — the roadmap is
    explicit that provider tool/vision/cache behaviour has not been confirmed
    against the live service. Capabilities are descriptive, not a hard gate.
    """

    model: str
    tools: bool | None = None
    parallel_tool_calls: bool | None = None
    vision: bool | None = None
    prompt_cache: bool | None = None
    streaming: bool | None = None
    max_context_tokens: int | None = None

    def known(self, name: str) -> bool:
        return getattr(self, name) is not None

    def unverified(self) -> list[str]:
        return [f.name for f in fields(self) if f.name != "model" and not self.known(f.name)]

    def to_dict(self) -> dict:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["unverified"] = self.unverified()
        return data


# --------------------------------------------------------------------------- #
# Usage normalisation and prefix fingerprinting
# --------------------------------------------------------------------------- #
def _first_int(raw: dict, paths: tuple[tuple[str, ...], ...]) -> int | None:
    for path in paths:
        current: object = raw
        for key in path:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            return int(current)
    return None


def normalize_usage(raw: dict | None) -> dict:
    """Preserve the provider usage dict, keeping ``cached_tokens``.

    Missing metrics become :data:`UNKNOWN` (never 0). ``cache_metrics_known``
    tells callers whether the provider actually reported cache telemetry, so a
    stable prefix hash is never mistaken for a cache hit.
    """
    usage = dict(raw) if isinstance(raw, dict) else {}
    prompt = _first_int(usage, (("prompt_tokens",), ("input_tokens",)))
    completion = _first_int(usage, (("completion_tokens",), ("output_tokens",)))
    total = _first_int(usage, (("total_tokens",),))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    cached = _first_int(usage, _CACHE_SOURCES)

    usage["prompt_tokens"] = prompt if prompt is not None else UNKNOWN
    usage["completion_tokens"] = completion if completion is not None else UNKNOWN
    usage["total_tokens"] = total if total is not None else UNKNOWN
    usage["cached_tokens"] = cached if cached is not None else UNKNOWN
    usage["cache_metrics_known"] = cached is not None
    usage["cache_hit_ratio"] = (
        round(cached / prompt, 4) if cached is not None and prompt else None
    )
    return usage


def _canonical(message: object) -> bytes:
    return json.dumps(
        message, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")


@dataclass(frozen=True)
class PrefixFingerprint:
    """Byte-level identity of the stable prefix and of the whole message list.

    ``prefix_hash`` covers the leading run of ``system`` messages (the persona +
    enabled-Skill catalogue). Dynamic turns must live *after* it, so a change in
    group chatter or affection leaves ``prefix_hash`` untouched while
    ``sequence_hash`` changes.
    """

    prefix_hash: str
    prefix_bytes: int
    sequence_hash: str
    message_count: int
    tools_hash: str


def prefix_fingerprint(
    messages: list[dict], tools: list[dict] | None = None
) -> PrefixFingerprint:
    prefix: list[dict] = []
    for message in messages:
        if message.get("role") != "system":
            break
        prefix.append(message)
    prefix_bytes = _canonical(prefix)
    sequence = "|".join(
        f"{m.get('role')}:{hashlib.sha256(_canonical(m)).hexdigest()[:12]}"
        for m in messages
    )
    tool_bytes = _canonical(tools or [])
    tools_hash = hashlib.sha256(tool_bytes).hexdigest()[:16]
    effective_prefix = prefix_bytes + b"\x00tools\x00" + tool_bytes
    return PrefixFingerprint(
        prefix_hash=hashlib.sha256(effective_prefix).hexdigest()[:16],
        prefix_bytes=len(effective_prefix),
        sequence_hash=hashlib.sha256(sequence.encode("utf-8")).hexdigest()[:16],
        message_count=len(messages),
        tools_hash=tools_hash,
    )


@dataclass(frozen=True)
class ModelCallMetrics:
    """Per-call telemetry. Contains no prompt or completion content."""

    prefix_hash: str
    prefix_bytes: int
    sequence_hash: str
    prefix_changed: bool
    message_count: int
    prompt_tokens: int | str
    completion_tokens: int | str
    cached_tokens: int | str
    cache_metrics_known: bool
    attempts: int
    duration_ms: int
    tools_offered: int
    tools_hash: str

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class ModelClient:
    """Minimal OpenAI-compatible chat adapter. Provider-specific adapters can replace it."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 90.0,
        reasoning_effort: str | None = None,
        capabilities: ModelCapabilities | None = None,
        retry: RetryPolicy | None = None,
        circuit: CircuitPolicy | None = None,
        budget: BudgetPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
        clock=time.monotonic,
        jitter_source=random.random,
        metrics_limit: int = 128,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = normalize_reasoning_effort(reasoning_effort)
        self.capabilities = capabilities or ModelCapabilities(model=model)
        self.retry = retry or RetryPolicy()
        self.circuit = circuit or CircuitPolicy()
        self.budget = budget or BudgetPolicy()
        self.client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter_source
        self._metrics: deque[ModelCallMetrics] = deque(maxlen=max(1, metrics_limit))
        self._last_prefix_hash: str | None = None
        self._prefix_changes = 0
        self._failures = 0
        self._opened_at: float | None = None
        self._request_times: deque[float] = deque()
        self._token_events: deque[tuple[float, int]] = deque()
        self.requests_sent = 0

    # -- observability ----------------------------------------------------- #
    def __repr__(self) -> str:  # never leak the key through repr/exception paths
        return (
            f"ModelClient(base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key={'***' if self.api_key else ''!r})"
        )

    @property
    def metrics(self) -> tuple[ModelCallMetrics, ...]:
        return tuple(self._metrics)

    def cache_report(self) -> dict:
        """Summarise prefix stability and provider-reported cache telemetry.

        ``provider_cache_hit`` is true only when the provider reported a
        positive ``cached_tokens`` count. An unchanged ``prefix_hash`` is
        reported separately and must not be read as a cache hit.
        """
        last = self._metrics[-1] if self._metrics else None
        return {
            "prefix_hash": last.prefix_hash if last else UNKNOWN,
            "prefix_stable": bool(last) and self._prefix_changes == 0,
            "prefix_changes": self._prefix_changes,
            "sequence_hash": last.sequence_hash if last else UNKNOWN,
            "prompt_tokens": last.prompt_tokens if last else UNKNOWN,
            "cached_tokens": last.cached_tokens if last else UNKNOWN,
            "cache_metrics_known": bool(last) and last.cache_metrics_known,
            "provider_cache_hit": bool(
                last
                and last.cache_metrics_known
                and isinstance(last.cached_tokens, int)
                and last.cached_tokens > 0
            ),
            "note": "prefix_hash equality does not prove a provider cache hit",
        }

    # -- policies ---------------------------------------------------------- #
    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        base = self.retry.base_for(attempt, retry_after=retry_after)
        if self.retry.jitter and base > 0:
            return base + base * self.retry.jitter * self._jitter()
        return base

    def _circuit_blocked(self, now: float) -> bool:
        if self._opened_at is None:
            return False
        if now - self._opened_at >= self.circuit.reset_after:
            # Cooldown elapsed: allow a single probe through.
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def _record_failure(self, now: float) -> None:
        self._failures += 1
        if self._failures >= self.circuit.failure_threshold:
            self._opened_at = now
            log.warning(
                "model circuit opened failures=%d reset_after=%.1fs",
                self._failures,
                self.circuit.reset_after,
            )

    def _record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def _prune(self, now: float) -> None:
        horizon = now - self.budget.window_seconds
        while self._request_times and self._request_times[0] < horizon:
            self._request_times.popleft()
        while self._token_events and self._token_events[0][0] < horizon:
            self._token_events.popleft()

    def _check_budget(self, now: float) -> None:
        if not self.budget.enabled:
            return
        self._prune(now)
        if (
            self.budget.max_requests is not None
            and len(self._request_times) >= self.budget.max_requests
        ):
            raise ModelBudgetExceeded(
                f"model request budget exhausted ({self.budget.max_requests} "
                f"per {self.budget.window_seconds:.0f}s)"
            )
        if self.budget.max_tokens is not None:
            spent = sum(tokens for _, tokens in self._token_events)
            if spent >= self.budget.max_tokens:
                raise ModelBudgetExceeded(
                    f"model token budget exhausted ({self.budget.max_tokens} "
                    f"per {self.budget.window_seconds:.0f}s)"
                )

    def _record_spend(self, now: float, tokens: int) -> None:
        self._request_times.append(now)
        if tokens:
            self._token_events.append((now, tokens))

    # -- transport --------------------------------------------------------- #
    async def _post(self, payload: dict) -> dict:
        self.requests_sent += 1
        try:
            response = await self.client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        except httpx.TimeoutException as exc:
            raise ModelTimeoutError(
                f"model request timed out ({type(exc).__name__})"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelTransientError(
                f"model transport error ({type(exc).__name__})"
            ) from exc

        status = response.status_code
        if status >= 400:
            raise _error_for(status, _retry_after(response))
        try:
            data = response.json()
        except ValueError as exc:
            # A proxy can return HTML/truncated bodies; treat as transient.
            raise ModelTransientError("model provider returned invalid JSON") from exc
        if not isinstance(data, dict) or "choices" not in data:
            # Valid JSON but not an OpenAI-compatible payload: contract breach.
            raise ModelPermanentError(
                "model response is missing the 'choices' field", status=status
            )
        return data

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.7,
    ) -> dict:
        if not self.api_key:
            raise ModelConfigError("BOT_MODEL_API_KEY is not configured")

        fingerprint = prefix_fingerprint(messages, tools)
        prefix_changed = (
            self._last_prefix_hash is not None
            and self._last_prefix_hash != fingerprint.prefix_hash
        )
        now = self._clock()
        if self._circuit_blocked(now):
            raise ModelCircuitOpenError(
                f"model circuit is open after {self._failures} consecutive failures"
            )
        self._check_budget(now)

        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        started = self._clock()
        last_error: ModelError | None = None
        for attempt in range(1, self.retry.max_attempts + 1):
            self._check_budget(self._clock())
            try:
                data = await self._post(payload)
            except ModelError as exc:
                last_error = exc
                if not exc.retryable or attempt >= self.retry.max_attempts:
                    self._record_failure(self._clock())
                    log.warning(
                        "model call failed kind=%s status=%s attempt=%d/%d"
                        " giving_up=true prefix_hash=%s",
                        exc.kind.value,
                        exc.status,
                        attempt,
                        self.retry.max_attempts,
                        fingerprint.prefix_hash,
                    )
                    raise exc.with_attempts(attempt)
                delay = self._backoff(attempt, exc.retry_after)
                log.warning(
                    "model call failed kind=%s status=%s attempt=%d/%d retry_in=%.2fs"
                    " prefix_hash=%s",
                    exc.kind.value,
                    exc.status,
                    attempt,
                    self.retry.max_attempts,
                    delay,
                    fingerprint.prefix_hash,
                )
                if delay:
                    await self._sleep(delay)
                continue

            usage = normalize_usage(data.get("usage"))
            tokens = usage["total_tokens"] if isinstance(usage["total_tokens"], int) else 0
            self._record_spend(self._clock(), tokens)
            self._record_success()
            self._last_prefix_hash = fingerprint.prefix_hash
            if prefix_changed:
                self._prefix_changes += 1
            self._metrics.append(
                ModelCallMetrics(
                    prefix_hash=fingerprint.prefix_hash,
                    prefix_bytes=fingerprint.prefix_bytes,
                    sequence_hash=fingerprint.sequence_hash,
                    prefix_changed=prefix_changed,
                    message_count=fingerprint.message_count,
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    cached_tokens=usage["cached_tokens"],
                    cache_metrics_known=usage["cache_metrics_known"],
                    attempts=attempt,
                    duration_ms=int((self._clock() - started) * 1000),
                    tools_offered=len(tools or ()),
                    tools_hash=fingerprint.tools_hash,
                )
            )
            data["usage"] = usage
            log.info(
                "model call ok model=%s attempts=%d prompt_tokens=%s cached_tokens=%s"
                " prefix_hash=%s prefix_changed=%s",
                self.model,
                attempt,
                usage["prompt_tokens"],
                usage["cached_tokens"],
                fingerprint.prefix_hash,
                prefix_changed,
            )
            return data

        # Unreachable: the loop either returns or raises. Kept for safety.
        raise last_error or ModelTransientError("model call failed")

    async def close(self) -> None:
        await self.client.aclose()
