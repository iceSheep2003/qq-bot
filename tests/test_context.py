"""The dynamic-suffix contract: budgets, priority, trust, failure isolation."""

from __future__ import annotations

import unittest

from qunbot.domain import MessageEvent
from qunbot.runtime.context import (
    ContextContribution,
    ContextRegistry,
    Trust,
    _shrink,
    cached,
)


def event(scope: str = "group:42") -> MessageEvent:
    group = scope.split(":", 1)[1] if scope.startswith("group:") else None
    return MessageEvent("e1", scope, group, "7", "小明", "你好", (), True, (), 0)


class RegistrationTests(unittest.TestCase):
    def test_duplicate_name_is_rejected(self):
        registry = ContextRegistry()
        registry.register("a", lambda _event: 1)
        with self.assertRaises(ValueError):
            registry.register("a", lambda _event: 2)

    def test_empty_name_is_rejected(self):
        with self.assertRaises(ValueError):
            ContextRegistry().register("", lambda _event: 1)


class BudgetTests(unittest.TestCase):
    def test_each_contribution_is_capped_by_its_own_budget(self):
        registry = ContextRegistry(budget_chars=10_000)
        registry.register("long", lambda _event: "x" * 5000, max_chars=100)
        self.assertEqual(len(registry.collect(event())["long"]), 100)

    def test_string_truncation_is_marked(self):
        registry = ContextRegistry(budget_chars=10_000)
        registry.register("long", lambda _event: "x" * 500, max_chars=50)
        value = registry.collect(event())["long"]
        self.assertEqual(len(value), 50)
        self.assertTrue(value.endswith("…（已截断）"))

    def test_low_priority_is_dropped_whole_when_the_budget_runs_out(self):
        registry = ContextRegistry(budget_chars=40)
        registry.register("keep", lambda _e: "a" * 30, priority=10, max_chars=100)
        registry.register("drop", lambda _e: "b" * 30, priority=90, max_chars=100)
        result = registry.collect(event())
        self.assertIn("keep", result)
        self.assertNotIn("drop", result)

    def test_priority_order_decides_who_survives_not_declaration_order(self):
        registry = ContextRegistry(budget_chars=40)
        registry.register("late", lambda _e: "b" * 30, priority=10, max_chars=100)
        registry.register("early", lambda _e: "a" * 30, priority=90, max_chars=100)
        self.assertEqual(list(registry.collect(event())), ["late"])

    def test_output_is_in_declaration_order_not_priority_order(self):
        registry = ContextRegistry(budget_chars=10_000)
        registry.register("second", lambda _e: "b", priority=10)
        registry.register("first", lambda _e: "a", priority=90)
        self.assertEqual(list(registry.collect(event())), ["second", "first"])

    def test_zero_budget_yields_nothing(self):
        registry = ContextRegistry(budget_chars=0)
        registry.register("a", lambda _e: "x" * 10)
        self.assertEqual(registry.collect(event()), {})

    def test_sequence_loses_whole_items(self):
        registry = ContextRegistry(budget_chars=10_000)
        registry.register("tags", lambda _e: ["aa", "bb", "cc"], max_chars=5)
        self.assertEqual(registry.collect(event())["tags"], ["aa", "bb"])


class ShrinkTests(unittest.TestCase):
    def test_non_positive_budget_yields_none(self):
        self.assertIsNone(_shrink("abc", 0))
        self.assertIsNone(_shrink("abc", -1))

    def test_dict_loses_whole_entries(self):
        self.assertEqual(_shrink({"a": "xx", "b": "yy"}, 3), {"a": "xx"})

    def test_short_payload_is_untouched(self):
        self.assertEqual(_shrink("abc", 10), "abc")


class TrustTests(unittest.TestCase):
    def test_trust_map_reports_every_contributor(self):
        registry = ContextRegistry()
        registry.register("raw", lambda _e: "x", trust=Trust.HOSTILE)
        registry.register("derived", lambda _e: "x", trust=Trust.DERIVED)
        registry.register("fixed", lambda _e: "x", trust=Trust.DEPLOYER)
        self.assertEqual(
            registry.trust_map(event()),
            {"raw": "low", "derived": "medium", "fixed": "high"},
        )

    def test_trust_ordering_is_meaningful(self):
        self.assertLess(Trust.HOSTILE, Trust.DERIVED)
        self.assertLess(Trust.DERIVED, Trust.DEPLOYER)


class FailureIsolationTests(unittest.TestCase):
    def test_a_raising_provider_does_not_break_the_turn(self):
        def explode(_event):
            raise RuntimeError("weather service is down")

        registry = ContextRegistry()
        registry.register("weather", explode)
        registry.register("mood", lambda _e: "还不错")
        result = registry.collect(event())
        self.assertNotIn("weather", result)
        self.assertEqual(result["mood"], "还不错")

    def test_a_raising_provider_is_marked_failed(self):
        def explode(_event):
            raise RuntimeError("nope")

        registry = ContextRegistry()
        registry.register("weather", explode)
        failed = [c for c in registry.contributions(event()) if c.failed]
        self.assertEqual([c.name for c in failed], ["weather"])


class CountingProvider:
    """A provider that records how often it was actually asked."""

    def __init__(self, answer=None):
        self.answer = answer
        self.calls = 0

    def __call__(self, _event):
        self.calls += 1
        return self.answer


class Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class CachedProviderTests(unittest.TestCase):
    """A provider runs inside a turn; recomputing it every turn is the waste."""

    def test_a_second_call_inside_the_window_is_served_from_cache(self):
        inner = CountingProvider("note")
        clock = Clock()
        provider = cached(inner, ttl_seconds=60, clock=clock)
        self.assertEqual(provider(event()), "note")
        self.assertEqual(provider(event()), "note")
        self.assertEqual(inner.calls, 1)

    def test_the_cache_expires(self):
        inner = CountingProvider("note")
        clock = Clock()
        provider = cached(inner, ttl_seconds=60, clock=clock)
        provider(event())
        clock.now = 59
        provider(event())
        clock.now = 61
        provider(event())
        self.assertEqual(inner.calls, 2)

    def test_scopes_do_not_share_an_answer(self):
        inner = CountingProvider("note")
        provider = cached(inner, ttl_seconds=60, clock=Clock())
        provider(event("group:1"))
        provider(event("group:2"))
        self.assertEqual(inner.calls, 2)

    def test_a_custom_key_separates_speakers_in_one_scope(self):
        """The style note describes the speaker, so it cannot be shared."""
        inner = CountingProvider("note")
        provider = cached(
            inner,
            ttl_seconds=60,
            key=lambda e: f"{e.scope}|{e.user_id}",
            clock=Clock(),
        )
        provider(event())
        provider(MessageEvent("e2", "group:42", "42", "9", "小红", "你好", (), True, (), 0))
        self.assertEqual(inner.calls, 2)

    def test_nothing_to_say_is_cached_too(self):
        inner = CountingProvider(None)
        provider = cached(inner, ttl_seconds=60, clock=Clock())
        self.assertIsNone(provider(event()))
        self.assertIsNone(provider(event()))
        self.assertEqual(inner.calls, 1)

    def test_a_raising_provider_is_not_pinned_for_the_window(self):
        class Flaky:
            def __init__(self):
                self.calls = 0

            def __call__(self, _event):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient")
                return "recovered"

        inner = Flaky()
        provider = cached(inner, ttl_seconds=60, clock=Clock())
        with self.assertRaises(RuntimeError):
            provider(event())
        self.assertEqual(provider(event()), "recovered")

    def test_a_zero_ttl_disables_the_cache(self):
        inner = CountingProvider("note")
        provider = cached(inner, ttl_seconds=0, clock=Clock())
        provider(event())
        provider(event())
        self.assertEqual(inner.calls, 2)

    def test_a_per_speaker_key_stays_bounded(self):
        """style_echo keys on scope+user, which is not a small set."""
        inner = CountingProvider("note")
        provider = cached(
            inner,
            ttl_seconds=600,
            key=lambda e: f"{e.scope}|{e.user_id}",
            max_entries=4,
            clock=Clock(),
        )
        for index in range(50):
            provider(event(f"group:{index}"))
        self.assertLessEqual(provider.cache_size(), 4)

    def test_a_registry_accepts_a_wrapped_provider_unchanged(self):
        registry = ContextRegistry()
        registry.register(
            "style_echo",
            cached(CountingProvider("note"), ttl_seconds=60, clock=Clock()),
            trust=Trust.DERIVED,
            priority=65,
            max_chars=250,
        )
        self.assertIn("style_echo", registry.names())


class ContributionTests(unittest.TestCase):
    def test_contribution_carries_its_scope(self):
        registry = ContextRegistry()
        registry.register("mood", lambda _e: "x")
        self.assertEqual(registry.contributions(event("group:99"))[0].scope, "group:99")

    def test_rendered_applies_the_contribution_budget(self):
        contribution = ContextContribution(
            name="n", payload="y" * 100, max_chars=10
        )
        self.assertEqual(len(contribution.rendered()), 10)


if __name__ == "__main__":
    unittest.main()
