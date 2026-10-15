from __future__ import annotations

import unittest

from qunbot.experience.compiler import BehaviorCompiler
from qunbot.experience.retrieval import BehaviorOption


def option(strategy: str, score: float, *, action: str = "joke", media: str = "none"):
    return BehaviorOption(
        strategy=strategy,
        dialogue_act=action,
        expression_pattern="短句接住当前话题",
        avoid=("不要长篇说教",),
        media_intent=media,
        support_case_count=10,
        examples=({"situation": "有人在群里接梗"},),
        score=score,
        snapshot_version="test-v1",
    )


class BehaviorCompilerTests(unittest.TestCase):
    def test_deduplicates_and_bounds_options(self):
        context = BehaviorCompiler(max_options=2, max_cases=1, char_budget=400).compile(
            [option("banter", 5), option("banter", 4), option("agree", 3), option("clarify", 2)]
        )
        self.assertEqual([item.strategy for item in context.options], ["banter", "agree"])
        self.assertEqual(context.omitted_options, 1)
        self.assertLessEqual(len(context.render()), 400)

    def test_empty_retrieval_is_safe(self):
        context = BehaviorCompiler().compile([])
        self.assertEqual(context.options, ())
        self.assertIn("没有命中", context.render())

    def test_conflicting_media_is_reported(self):
        context = BehaviorCompiler().compile(
            [option("banter", 5, media="meme"), option("banter", 4, media="native_reaction")]
        )
        self.assertTrue(context.conflicts)


if __name__ == "__main__":
    unittest.main()
