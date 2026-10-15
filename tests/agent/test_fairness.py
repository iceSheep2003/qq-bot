import unittest

from qunbot.agent.fairness import ScopeWorkBudget


class ScopeWorkBudgetTests(unittest.TestCase):
    def test_budget_is_independent_per_scope_and_expires(self):
        now = [0.0]
        budget = ScopeWorkBudget(max_per_scope=2, window_seconds=10, clock=lambda: now[0])
        self.assertTrue(budget.admit("group:1"))
        self.assertTrue(budget.admit("group:1"))
        self.assertFalse(budget.admit("group:1"))
        self.assertTrue(budget.admit("group:2"))
        now[0] = 11.0
        self.assertTrue(budget.admit("group:1"))

    def test_invalid_limits_fail_fast(self):
        with self.assertRaises(ValueError):
            ScopeWorkBudget(max_per_scope=0)
        with self.assertRaises(ValueError):
            ScopeWorkBudget(window_seconds=0)
