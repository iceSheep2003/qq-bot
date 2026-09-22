"""Tests for the model-provider adapter, driven by a local fake server.

No real model service is contacted: every case runs against an
``httpx.MockTransport`` handler. Error classification, retry/backoff, the
circuit breaker, the call budget, usage/cache normalisation and the tool-call
pass-through protocol are all exercised here.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from qunbot.adapters.model import (
    UNKNOWN,
    BudgetPolicy,
    CircuitPolicy,
    ErrorKind,
    ModelBudgetExceeded,
    ModelCallMetrics,
    ModelCapabilities,
    ModelCircuitOpenError,
    ModelClient,
    ModelConfigError,
    ModelPermanentError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelTransientError,
    PrefixFingerprint,
    RetryPolicy,
    classify_status,
    normalize_usage,
    prefix_fingerprint,
    redact,
)

SECRET = "sk-live-super-secret-key"
PROMPT_MARKER = "SENSITIVE-PROMPT-MARKER"


class FakeProvider:
    """Scripted OpenAI-compatible endpoint. Records every request it receives."""

    def __init__(self, *responses):
        self.script = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.script:
            return httpx.Response(500, json={"error": "no scripted response"})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def payloads(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]

    @property
    def count(self) -> int:
        return len(self.requests)


def ok(content="好的", *, usage=None, tool_calls=None) -> httpx.Response:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
    body = {
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return httpx.Response(200, json=body)


def error_response(status: int, **headers) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": "nope"}}, headers=headers)


def make_client(provider: FakeProvider, **kwargs):
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    client = ModelClient(
        "https://fake.example/v1",
        SECRET,
        "test-model",
        transport=httpx.MockTransport(provider.handler),
        sleep=sleep,
        jitter_source=lambda: 0.0,
        **kwargs,
    )
    return client, delays


def run(coro):
    return asyncio.run(coro)


def messages(prefix: str = "固定人格", tail: str = "你好") -> list[dict]:
    return [
        {"role": "system", "content": prefix},
        {"role": "user", "content": tail},
    ]


class ErrorTaxonomyTests(unittest.TestCase):
    def test_status_classification(self):
        cases = {
            408: ErrorKind.TIMEOUT,
            429: ErrorKind.RATE_LIMIT,
            500: ErrorKind.TRANSIENT,
            502: ErrorKind.TRANSIENT,
            503: ErrorKind.TRANSIENT,
            400: ErrorKind.PERMANENT,
            401: ErrorKind.PERMANENT,
            403: ErrorKind.PERMANENT,
            404: ErrorKind.PERMANENT,
            422: ErrorKind.PERMANENT,
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertIs(classify_status(status), expected)

    def test_only_transient_kinds_are_retryable(self):
        self.assertTrue(ModelTimeoutError("x").retryable)
        self.assertTrue(ModelRateLimitError("x").retryable)
        self.assertTrue(ModelTransientError("x").retryable)
        self.assertFalse(ModelPermanentError("x").retryable)
        self.assertFalse(ModelBudgetExceeded("x").retryable)
        self.assertFalse(ModelCircuitOpenError("x").retryable)


class UsageNormalizationTests(unittest.TestCase):
    def test_openai_cached_tokens_are_preserved(self):
        usage = normalize_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 64},
                "custom_provider_field": "kept",
            }
        )
        self.assertEqual(usage["cached_tokens"], 64)
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["total_tokens"], 120)
        self.assertTrue(usage["cache_metrics_known"])
        self.assertEqual(usage["cache_hit_ratio"], 0.64)
        self.assertEqual(usage["custom_provider_field"], "kept")

    def test_deepseek_and_anthropic_cache_fields(self):
        deepseek = normalize_usage(
            {"prompt_tokens": 50, "completion_tokens": 5, "prompt_cache_hit_tokens": 32}
        )
        self.assertEqual(deepseek["cached_tokens"], 32)
        self.assertEqual(deepseek["total_tokens"], 55)
        anthropic = normalize_usage(
            {"input_tokens": 40, "output_tokens": 4, "cache_read_input_tokens": 8}
        )
        self.assertEqual(anthropic["prompt_tokens"], 40)
        self.assertEqual(anthropic["completion_tokens"], 4)
        self.assertEqual(anthropic["cached_tokens"], 8)

    def test_missing_cache_metric_is_unknown_not_zero(self):
        usage = normalize_usage({"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual(usage["cached_tokens"], UNKNOWN)
        self.assertIsNot(usage["cached_tokens"], 0)
        self.assertFalse(usage["cache_metrics_known"])
        self.assertIsNone(usage["cache_hit_ratio"])

    def test_absent_usage_is_unknown(self):
        usage = normalize_usage(None)
        self.assertEqual(usage["prompt_tokens"], UNKNOWN)
        self.assertEqual(usage["cached_tokens"], UNKNOWN)
        self.assertFalse(usage["cache_metrics_known"])


class PrefixFingerprintTests(unittest.TestCase):
    def test_stable_prefix_ignores_dynamic_tail(self):
        first = prefix_fingerprint(
            [{"role": "system", "content": "人格"}, {"role": "user", "content": "群消息A"}]
        )
        second = prefix_fingerprint(
            [
                {"role": "system", "content": "人格"},
                {"role": "user", "content": "完全不同的一轮：好感度=88 记忆=..."},
            ]
        )
        self.assertEqual(first.prefix_hash, second.prefix_hash)
        self.assertEqual(first.prefix_bytes, second.prefix_bytes)
        self.assertNotEqual(first.sequence_hash, second.sequence_hash)

    def test_changed_persona_changes_prefix(self):
        one = prefix_fingerprint(messages(prefix="人格A"))
        two = prefix_fingerprint(messages(prefix="人格B"))
        self.assertNotEqual(one.prefix_hash, two.prefix_hash)

    def test_fingerprint_shape(self):
        fingerprint = prefix_fingerprint(messages())
        self.assertIsInstance(fingerprint, PrefixFingerprint)
        self.assertEqual(fingerprint.message_count, 2)
        self.assertEqual(len(fingerprint.prefix_hash), 16)


class SuccessPathTests(unittest.TestCase):
    def test_openai_shape_is_returned(self):
        provider = FakeProvider(ok("你好呀"))
        client, _ = make_client(provider)
        try:
            result = run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(result["choices"][0]["message"]["content"], "你好呀")
        self.assertEqual(result["usage"]["prompt_tokens"], 10)
        self.assertEqual(result["usage"]["cached_tokens"], UNKNOWN)

    def test_request_payload_carries_model_and_temperature(self):
        provider = FakeProvider(ok())
        client, _ = make_client(provider)
        try:
            run(client.complete(messages(), temperature=0.25))
        finally:
            run(client.close())
        payload = provider.payloads[0]
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["temperature"], 0.25)
        self.assertNotIn("tools", payload)
        self.assertNotIn(SECRET, provider.requests[0].content.decode())

    def test_messages_are_not_mutated(self):
        provider = FakeProvider(ok())
        client, _ = make_client(provider)
        original = messages(tail=PROMPT_MARKER)
        snapshot = json.dumps(original, sort_keys=True)
        try:
            run(client.complete(original))
        finally:
            run(client.close())
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)

    def test_metrics_record_prefix_and_usage(self):
        provider = FakeProvider(
            ok(usage={"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101,
                      "prompt_tokens_details": {"cached_tokens": 90}}),
            ok(usage={"prompt_tokens": 110, "completion_tokens": 1, "total_tokens": 111,
                      "prompt_tokens_details": {"cached_tokens": 0}}),
        )
        client, _ = make_client(provider)
        try:
            run(client.complete(messages(tail="第一轮，好感度=1")))
            run(client.complete(messages(tail="第二轮，好感度=2")))
        finally:
            run(client.close())
        first, second = client.metrics
        self.assertIsInstance(first, ModelCallMetrics)
        self.assertEqual(first.prefix_hash, second.prefix_hash)
        self.assertFalse(second.prefix_changed)
        self.assertNotEqual(first.sequence_hash, second.sequence_hash)
        self.assertEqual(client.cache_report()["cached_tokens"], 0)
        self.assertFalse(client.cache_report()["provider_cache_hit"])

    def test_cache_report_marks_hit_only_from_provider_usage(self):
        provider = FakeProvider(
            ok(usage={"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101,
                      "prompt_tokens_details": {"cached_tokens": 80}})
        )
        client, _ = make_client(provider)
        try:
            run(client.complete(messages()))
            report = client.cache_report()
        finally:
            run(client.close())
        self.assertTrue(report["provider_cache_hit"])
        self.assertEqual(report["cached_tokens"], 80)
        self.assertTrue(report["cache_metrics_known"])

    def test_cache_report_without_provider_field_says_unknown(self):
        provider = FakeProvider(ok(usage={"prompt_tokens": 10, "completion_tokens": 1}))
        client, _ = make_client(provider)
        try:
            run(client.complete(messages()))
            report = client.cache_report()
        finally:
            run(client.close())
        self.assertEqual(report["cached_tokens"], UNKNOWN)
        self.assertFalse(report["cache_metrics_known"])
        self.assertFalse(report["provider_cache_hit"])
        self.assertIn("does not prove", report["note"])

    def test_prefix_change_is_tracked(self):
        provider = FakeProvider(ok(), ok())
        client, _ = make_client(provider)
        try:
            run(client.complete(messages(prefix="人格A")))
            run(client.complete(messages(prefix="人格B")))
        finally:
            run(client.close())
        self.assertEqual(client.cache_report()["prefix_changes"], 1)
        self.assertFalse(client.cache_report()["prefix_stable"])


class ErrorPathTests(unittest.TestCase):
    def test_timeout_is_retried_then_succeeds(self):
        provider = FakeProvider(httpx.ReadTimeout("slow"), ok("恢复了"))
        client, delays = make_client(provider)
        try:
            result = run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(result["choices"][0]["message"]["content"], "恢复了")
        self.assertEqual(provider.count, 2)
        self.assertEqual(delays, [0.5])
        self.assertEqual(client.metrics[-1].attempts, 2)

    def test_rate_limit_honours_retry_after(self):
        provider = FakeProvider(error_response(429, **{"Retry-After": "2"}), ok())
        client, delays = make_client(provider)
        try:
            run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(delays, [2.0])

    def test_retry_after_is_capped_by_max_delay(self):
        provider = FakeProvider(error_response(429, **{"Retry-After": "600"}), ok())
        client, delays = make_client(provider, retry=RetryPolicy(max_delay=8.0))
        try:
            run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(delays, [8.0])

    def test_transient_error_exhausts_attempts(self):
        provider = FakeProvider(
            error_response(500), error_response(502), error_response(503)
        )
        client, delays = make_client(provider, retry=RetryPolicy(max_attempts=3))
        try:
            with self.assertRaises(ModelTransientError) as ctx:
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(ctx.exception.attempts, 3)
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(provider.count, 3)
        self.assertEqual(delays, [0.5, 1.0])

    def test_permanent_error_is_not_retried(self):
        provider = FakeProvider(error_response(401))
        client, delays = make_client(provider)
        try:
            with self.assertRaises(ModelPermanentError) as ctx:
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(ctx.exception.attempts, 1)
        self.assertEqual(provider.count, 1)
        self.assertEqual(delays, [])

    def test_bad_request_is_permanent(self):
        provider = FakeProvider(error_response(400))
        client, _ = make_client(provider)
        try:
            with self.assertRaises(ModelPermanentError):
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(provider.count, 1)

    def test_invalid_json_is_transient(self):
        provider = FakeProvider(
            httpx.Response(200, content=b"<html>gateway</html>"),
            ok(),
        )
        client, _ = make_client(provider)
        try:
            result = run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(provider.count, 2)
        self.assertEqual(result["choices"][0]["message"]["content"], "好的")

    def test_missing_choices_is_permanent(self):
        provider = FakeProvider(httpx.Response(200, json={"weird": True}))
        client, _ = make_client(provider)
        try:
            with self.assertRaises(ModelPermanentError):
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(provider.count, 1)

    def test_missing_api_key_is_config_error(self):
        provider = FakeProvider(ok())
        client, _ = make_client(provider)
        client.api_key = ""
        try:
            with self.assertRaises(ModelConfigError):
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(provider.count, 0)


class CircuitBreakerTests(unittest.TestCase):
    def test_circuit_opens_after_threshold_and_fails_fast(self):
        provider = FakeProvider(error_response(500), error_response(500))
        client, _ = make_client(
            provider,
            retry=RetryPolicy(max_attempts=1),
            circuit=CircuitPolicy(failure_threshold=2, reset_after=30.0),
        )
        try:
            for _ in range(2):
                with self.assertRaises(ModelTransientError):
                    run(client.complete(messages()))
            with self.assertRaises(ModelCircuitOpenError):
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(provider.count, 2)

    def test_circuit_probes_again_after_cooldown(self):
        now = [0.0]
        provider = FakeProvider(error_response(500), error_response(500), ok())
        client, _ = make_client(
            provider,
            retry=RetryPolicy(max_attempts=1),
            circuit=CircuitPolicy(failure_threshold=2, reset_after=30.0),
            clock=lambda: now[0],
        )
        try:
            for _ in range(2):
                with self.assertRaises(ModelTransientError):
                    run(client.complete(messages()))
            with self.assertRaises(ModelCircuitOpenError):
                run(client.complete(messages()))
            now[0] = 31.0
            result = run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(result["choices"][0]["message"]["content"], "好的")
        self.assertEqual(provider.count, 3)

    def test_success_resets_failure_streak(self):
        provider = FakeProvider(error_response(500), ok(), error_response(500), ok())
        client, _ = make_client(
            provider,
            retry=RetryPolicy(max_attempts=1),
            circuit=CircuitPolicy(failure_threshold=2),
        )
        try:
            with self.assertRaises(ModelTransientError):
                run(client.complete(messages()))
            run(client.complete(messages()))
            with self.assertRaises(ModelTransientError):
                run(client.complete(messages()))
            result = run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(result["choices"][0]["message"]["content"], "好的")


class BudgetTests(unittest.TestCase):
    def test_request_budget_blocks_before_calling_provider(self):
        provider = FakeProvider(ok(), ok())
        client, _ = make_client(
            provider,
            retry=RetryPolicy(max_attempts=1),
            budget=BudgetPolicy(max_requests=2, window_seconds=60.0),
        )
        try:
            run(client.complete(messages()))
            run(client.complete(messages()))
            with self.assertRaises(ModelBudgetExceeded):
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(provider.count, 2)

    def test_token_budget_blocks_after_spend(self):
        spend = {"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20}
        provider = FakeProvider(ok(usage=spend), ok(usage=spend))
        client, _ = make_client(
            provider,
            retry=RetryPolicy(max_attempts=1),
            budget=BudgetPolicy(max_tokens=20, window_seconds=60.0),
        )
        try:
            run(client.complete(messages()))
            with self.assertRaises(ModelBudgetExceeded):
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(provider.count, 1)

    def test_budget_window_expiry_restores_capacity(self):
        now = [0.0]
        provider = FakeProvider(ok(), ok())
        client, _ = make_client(
            provider,
            retry=RetryPolicy(max_attempts=1),
            budget=BudgetPolicy(max_requests=1, window_seconds=60.0),
            clock=lambda: now[0],
        )
        try:
            run(client.complete(messages()))
            with self.assertRaises(ModelBudgetExceeded):
                run(client.complete(messages()))
            now[0] = 61.0
            run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertEqual(provider.count, 2)

    def test_default_budget_is_unbounded(self):
        provider = FakeProvider(*[ok() for _ in range(5)])
        client, _ = make_client(provider)
        try:
            for _ in range(5):
                run(client.complete(messages()))
        finally:
            run(client.close())
        self.assertFalse(client.budget.enabled)


class ToolProtocolTests(unittest.TestCase):
    def test_tool_calls_pass_through_and_tools_are_forwarded(self):
        calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "recall_memory", "arguments": '{"query": "x"}'},
            }
        ]
        provider = FakeProvider(ok(tool_calls=calls), ok("查到了"))
        client, _ = make_client(provider)
        schema = [{"type": "function", "function": {"name": "recall_memory"}}]
        try:
            first = run(client.complete(messages(), schema))
            second = run(client.complete(messages(), schema))
        finally:
            run(client.close())
        self.assertEqual(first["choices"][0]["message"]["tool_calls"], calls)
        self.assertEqual(second["choices"][0]["message"]["content"], "查到了")
        payload = provider.payloads[0]
        self.assertEqual(payload["tools"], schema)
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(provider.payloads[1]["tools"], schema)


class CapabilityTests(unittest.TestCase):
    def test_default_capabilities_are_unverified_not_false(self):
        capabilities = ModelCapabilities(model="grok")
        self.assertIsNone(capabilities.tools)
        self.assertFalse(capabilities.known("prompt_cache"))
        self.assertIn("prompt_cache", capabilities.unverified())
        self.assertEqual(capabilities.to_dict()["tools"], None)

    def test_declared_capabilities_are_marked_verified(self):
        capabilities = ModelCapabilities(
            model="grok", tools=True, prompt_cache=True, vision=False
        )
        self.assertEqual(capabilities.to_dict()["tools"], True)
        self.assertEqual(capabilities.to_dict()["vision"], False)
        self.assertEqual(capabilities.unverified(), ["parallel_tool_calls", "streaming", "max_context_tokens"])

    def test_client_exposes_capabilities(self):
        provider = FakeProvider(ok())
        client, _ = make_client(
            provider, capabilities=ModelCapabilities(model="test-model", tools=True)
        )
        try:
            self.assertTrue(client.capabilities.tools)
            self.assertEqual(client.capabilities.model, "test-model")
        finally:
            run(client.close())
        default_provider = FakeProvider(ok())
        default_client, _ = make_client(default_provider)
        try:
            self.assertEqual(default_client.capabilities.model, "test-model")
            self.assertIn("vision", default_client.capabilities.unverified())
        finally:
            run(default_client.close())


class SecretHygieneTests(unittest.TestCase):
    def test_key_and_prompt_never_reach_logs(self):
        provider = FakeProvider(error_response(500))
        client, _ = make_client(provider, retry=RetryPolicy(max_attempts=1))
        try:
            with self.assertLogs("qunbot.adapters.model", level="INFO") as captured:
                with self.assertRaises(ModelTransientError):
                    run(client.complete(messages(prefix=PROMPT_MARKER)))
        finally:
            run(client.close())
        blob = "\n".join(captured.output)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn(PROMPT_MARKER, blob)

    def test_key_never_reaches_exception_message(self):
        provider = FakeProvider(error_response(401))
        client, _ = make_client(provider)
        try:
            with self.assertRaises(ModelPermanentError) as ctx:
                run(client.complete(messages(prefix=PROMPT_MARKER)))
        finally:
            run(client.close())
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertNotIn(PROMPT_MARKER, str(ctx.exception))

    def test_repr_redacts_api_key(self):
        provider = FakeProvider()
        client, _ = make_client(provider)
        try:
            self.assertNotIn(SECRET, repr(client))
            self.assertIn("***", repr(client))
        finally:
            run(client.close())

    def test_redact_helper(self):
        text = redact(f"key={SECRET} body={PROMPT_MARKER}", SECRET)
        self.assertNotIn(SECRET, text)
        self.assertIn(PROMPT_MARKER, text)
        self.assertLessEqual(len(redact("x" * 500, SECRET)), 201)


if __name__ == "__main__":
    unittest.main()
