"""The dynamic-suffix contract: budgets, priority, trust, failure isolation."""

from __future__ import annotations

import unittest

from qunbot.domain import MessageEvent
from qunbot.runtime.context import (
    ContextContribution,
    ContextRegistry,
    Trust,
    _shrink,
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
