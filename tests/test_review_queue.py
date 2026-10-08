"""The owner console's one write surface outside the env editor.

The console edits the review *file*, and the feature reads its decisions from
that file and nowhere else. So the file is the whole contract, and the tests
here are mostly about what the editor refuses to do to it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from qunbot.extensions.webui.review_queue import ReviewFileEditor, ReviewFormatError


class ReviewEditorTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config" / "slang_review.json"
        self.editor = ReviewFileEditor(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def written(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def seed(self, payload) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class ReadTests(ReviewEditorTestCase):
    def test_a_missing_file_reads_as_nothing_yet(self):
        self.assertEqual(self.editor.load(), {})

    def test_an_empty_file_reads_as_nothing_yet(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("   \n", encoding="utf-8")
        self.assertEqual(self.editor.load(), {})

    def test_a_malformed_file_is_refused_not_guessed(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ReviewFormatError):
            self.editor.load()

    def test_a_non_object_root_is_refused(self):
        self.seed(["approve"])
        with self.assertRaises(ReviewFormatError):
            self.editor.load()

    def test_the_summary_counts_both_levels(self):
        self.seed(
            {
                "approve": ["甲"],
                "meanings": {"甲": "说明"},
                "scopes": {"group:42": {"reject": ["乙", "丙"]}},
            }
        )
        summary = self.editor.summary()
        self.assertEqual(summary["counts"]["approve"], 1)
        self.assertEqual(summary["counts"]["reject"], 2)
        self.assertEqual(summary["counts"]["meanings"], 1)


class DecisionTests(ReviewEditorTestCase):
    def test_a_decision_is_written_at_the_top_level(self):
        self.editor.set_decision("上大分", "approve")
        self.assertEqual(self.written()["approve"], ["上大分"])

    def test_deciding_again_moves_the_term_rather_than_duplicating_it(self):
        self.editor.set_decision("上大分", "approve")
        self.editor.set_decision("上大分", "reject")
        document = self.written()
        self.assertNotIn("approve", document)
        self.assertEqual(document["reject"], ["上大分"])

    def test_reset_clears_both_other_lists(self):
        self.editor.set_decision("上大分", "approve")
        self.editor.set_decision("上大分", "reset")
        document = self.written()
        self.assertNotIn("approve", document)
        self.assertEqual(document["reset"], ["上大分"])

    def test_a_decision_can_be_scoped_to_one_group(self):
        self.editor.set_decision("上大分", "approve", scope="group:42")
        document = self.written()
        self.assertNotIn("approve", document)
        self.assertEqual(document["scopes"]["group:42"]["approve"], ["上大分"])

    def test_a_scope_and_the_top_level_do_not_interfere(self):
        self.editor.set_decision("甲", "approve")
        self.editor.set_decision("乙", "approve", scope="group:42")
        document = self.written()
        self.assertEqual(document["approve"], ["甲"])
        self.assertEqual(document["scopes"]["group:42"]["approve"], ["乙"])

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            self.editor.set_decision("上大分", "trusted")

    def test_an_empty_term_is_refused(self):
        with self.assertRaises(ValueError):
            self.editor.set_decision("   ", "approve")


class MeaningTests(ReviewEditorTestCase):
    def test_a_meaning_is_written(self):
        self.editor.set_meaning("上大分", "赢了、拿到好处")
        self.assertEqual(self.written()["meanings"]["上大分"], "赢了、拿到好处")

    def test_an_empty_meaning_removes_the_entry(self):
        self.editor.set_meaning("上大分", "赢了")
        self.editor.set_meaning("上大分", "")
        self.assertNotIn("meanings", self.written())

    def test_null_is_written_as_the_release_signal(self):
        """`null` is how a deployer hands a term back to automatic inference."""
        self.editor.set_meaning("上大分", "人工写法")
        self.editor.set_meaning("上大分", None)
        self.assertIsNone(self.written()["meanings"]["上大分"])

    def test_a_meaning_can_be_scoped(self):
        self.editor.set_meaning("上大分", "本群说法", scope="group:42")
        self.assertEqual(
            self.written()["scopes"]["group:42"]["meanings"]["上大分"], "本群说法"
        )


class ForgetTests(ReviewEditorTestCase):
    def test_forget_removes_every_mention_of_a_term(self):
        self.editor.set_decision("上大分", "approve")
        self.editor.set_meaning("上大分", "赢了")
        self.editor.set_decision("上大分", "reject", scope="group:42")
        self.editor.set_meaning("上大分", "本群说法", scope="group:42")
        self.editor.set_decision("别的词", "approve")

        self.editor.forget("上大分")
        document = self.written()
        self.assertEqual(document.get("approve"), ["别的词"])
        self.assertNotIn("meanings", document)
        self.assertNotIn("scopes", document)

    def test_forget_leaves_the_other_terms_alone(self):
        self.editor.set_decision("甲", "approve")
        self.editor.set_decision("乙", "approve")
        self.editor.forget("甲")
        self.assertEqual(self.written()["approve"], ["乙"])


class SafetyTests(ReviewEditorTestCase):
    def test_a_malformed_file_is_never_overwritten(self):
        """The feature keeps its last good decisions when the file is broken,
        so writing over one would look like the console had wiped them."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ReviewFormatError):
            self.editor.set_decision("上大分", "approve")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_a_wrongly_shaped_list_is_refused(self):
        self.seed({"approve": "上大分"})
        with self.assertRaises(ReviewFormatError):
            self.editor.set_decision("别的词", "approve")

    def test_the_written_file_is_readable_json_with_no_leftovers(self):
        self.editor.set_decision("上大分", "approve")
        self.editor.set_meaning("上大分", "赢了")
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(json.loads(text)["approve"], ["上大分"])
        # No temp file left beside it.
        self.assertEqual(
            sorted(child.name for child in self.path.parent.iterdir()),
            ["slang_review.json"],
        )

    def test_the_file_the_feature_reads_is_the_file_this_writes(self):
        """The console must not become a second source of truth.

        Round-tripping through the extension's own parser is the check that
        matters: whatever the console writes has to be a document the feature
        would accept.
        """
        from qunbot.extensions.slang.review import load_review

        self.editor.set_decision("上大分", "approve")
        self.editor.set_meaning("上大分", "赢了、拿到好处")
        review = load_review(self.path)
        self.assertIsNotNone(review.status_for("group:42", "上大分"))
        self.assertEqual(review.meaning_for("group:42", "上大分"), "赢了、拿到好处")
