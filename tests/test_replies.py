"""Reply modality planning is deterministic and independent from the agent."""

from __future__ import annotations

import asyncio
from pathlib import Path
import json
import tempfile
import unittest

from qunbot.domain import MessageEvent
from qunbot.extensions.media import ReplyMediaProcessor
from qunbot.extensions.memes.catalog import LocalMemeCatalog
from qunbot.replies import (
    DefaultReplyPlanner,
    ReplyCapabilities,
    ReplyDispatcher,
    ReplyDraft,
    QQFacePolicy,
    HumanizedPacer,
    TextSegmenter,
    parse_reply_draft,
)
from qunbot.replies.quotes import QuotePolicy
from qunbot.replies.media_policy import CasualMediaPolicy
from qunbot.replies.repetition import repeats_recent


class RepetitionGuardTests(unittest.TestCase):
    def test_blocks_member_echo_and_old_bot_catchphrase(self):
        rows = [
            {"role": "user", "content": "感觉挺有意思的，我能玩一天"},
            {"role": "assistant", "content": "戳到啦，群里太欢乐了哈哈"},
        ]
        self.assertTrue(repeats_recent("哈哈，感觉挺有意思的，我能玩一天呀", rows))
        self.assertTrue(repeats_recent("戳到啦，群里太欢乐了哈哈。", rows))
        self.assertFalse(repeats_recent("这个玩法容易一不留神就到半夜", rows))


class Sender:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return {}


class Memes:
    def __init__(self, values=None):
        self.values = values or {}

    def pick(self, tag):
        return self.values.get(tag)


class Speech:
    def __init__(self, source="http://tts.test/reply.mp3"):
        self.source = source
        self.seen: list[str] = []

    async def synthesize(self, text):
        self.seen.append(text)
        return self.source


def event() -> MessageEvent:
    return MessageEvent(
        "1", "group:42", "42", "10001", "小明", "你好", (), True,
        ("10002",), 0,
    )


class ReplyParsingTests(unittest.TestCase):
    def test_structured_combination_is_parsed(self):
        raw = json.dumps(
            {
                "text": "笑死",
                "channels": ["voice", "meme", "text", "invalid"],
                "meme_tag": "开心",
                "voice_text": "笑死",
                "at_user_id": "10002",
                "conversation_decision": {
                    "target_message_id": "88",
                    "relation": "joke",
                    "confidence": 0.84,
                },
                "state_observations": {
                    "relationship": {"delta": 1, "reason": "友好互动"},
                    "mood": {"deltas": {"valence": 2}, "reason": "受到鼓励"},
                },
            },
            ensure_ascii=False,
        )
        draft = parse_reply_draft(raw)
        self.assertEqual(draft.channels, ("text", "meme", "voice"))
        self.assertEqual((draft.meme_tag, draft.at_user_id), ("开心", "10002"))
        self.assertEqual(draft.state_observations["relationship"]["delta"], 1)
        self.assertEqual(draft.conversation_decision["relation"], "joke")

    def test_malformed_output_degrades_to_plain_text(self):
        draft = parse_reply_draft("普通回复")
        self.assertEqual((draft.text, draft.channels), ("普通回复", ("text",)))

    def test_voice_style_is_bounded_to_known_values(self):
        styled = parse_reply_draft('{"text":"来啦","channels":["voice"],"voice_style":"warm"}')
        invalid = parse_reply_draft('{"text":"来啦","channels":["voice"],"voice_style":"shouting"}')
        self.assertEqual(styled.voice_style, "warm")
        self.assertEqual(invalid.voice_style, "neutral")

    def test_truncated_protocol_recovers_only_visible_text(self):
        draft = parse_reply_draft(
            '{"text":"这条正常发出去","channels":["text"],'
            '"state_observations":{"mood":{"reason":"内部状态"}}'
        )
        self.assertEqual((draft.text, draft.channels), ("这条正常发出去", ("text",)))
        self.assertNotIn("state_observations", draft.text)

    def test_broken_protocol_without_complete_text_fails_closed(self):
        draft = parse_reply_draft('{"text":"半截回复')
        self.assertEqual((draft.text, draft.channels), ("", ()))


class ReplyPlanningTests(unittest.TestCase):
    def test_planner_caps_model_requested_voice_with_same_probability_gate(self):
        planner = DefaultReplyPlanner(
            ReplyCapabilities(voice=True),
            casual_media=CasualMediaPolicy(
                voice_probability=0.20,
                followup_meme_probability=0,
                random_value=lambda: 0.30,
            ),
        )
        plan = asyncio.run(planner.plan(ReplyDraft("先休息一下", ("voice",)), event()))
        self.assertEqual(plan.channels, ("text",))
        self.assertEqual(plan.text, "先休息一下")

    def test_planner_caps_voice_only_draft_without_losing_spoken_words(self):
        planner = DefaultReplyPlanner(
            ReplyCapabilities(voice=True),
            casual_media=CasualMediaPolicy(
                voice_probability=0.20,
                followup_meme_probability=0,
                random_value=lambda: 0.30,
            ),
        )
        plan = asyncio.run(planner.plan(ReplyDraft("", ("voice",), voice_text="先休息一下"), event()))
        self.assertEqual((plan.channels, plan.text), (("text",), "先休息一下"))

    def test_casual_short_reply_can_be_voice_only(self):
        planner = DefaultReplyPlanner(
            ReplyCapabilities(voice=True),
            casual_media=CasualMediaPolicy(voice_probability=1, random_value=lambda: 0),
        )
        plan = asyncio.run(planner.plan(ReplyDraft("你这话有点东西呀"), event()))
        self.assertEqual(plan.channels, ("voice",))

    def test_matching_meme_can_follow_text(self):
        planner = DefaultReplyPlanner(
            ReplyCapabilities(meme_tags=frozenset({"震惊"})),
            casual_media=CasualMediaPolicy(followup_meme_probability=1, random_value=lambda: 0),
        )
        plan = asyncio.run(planner.plan(ReplyDraft("真的假的"), event()))
        self.assertEqual(plan.channels, ("text", "meme"))
        self.assertEqual(plan.meme_tag, "震惊")

    def test_casual_media_does_not_force_a_mismatched_meme_or_precise_voice(self):
        planner = DefaultReplyPlanner(
            ReplyCapabilities(meme_tags=frozenset({"震惊"}), voice=True),
            casual_media=CasualMediaPolicy(1, 1, random_value=lambda: 0),
        )
        plan = asyncio.run(planner.plan(ReplyDraft("今年分数线以官网为准"), event()))
        self.assertEqual(plan.channels, ("text",))

    def test_random_casual_meme_can_follow_without_keyword_match(self):
        planner = DefaultReplyPlanner(
            ReplyCapabilities(meme_tags=frozenset({"吃瓜"})),
            casual_media=CasualMediaPolicy(
                voice_probability=0,
                followup_meme_probability=0,
                random_meme_probability=1,
                random_value=lambda: 0,
            ),
        )
        plan = asyncio.run(planner.plan(ReplyDraft("你们继续，我看看"), event()))
        self.assertEqual(plan.channels, ("text", "meme"))
        self.assertEqual(plan.meme_tag, "吃瓜")

    def test_random_casual_meme_skips_precise_answer(self):
        planner = DefaultReplyPlanner(
            ReplyCapabilities(meme_tags=frozenset({"吃瓜"})),
            casual_media=CasualMediaPolicy(
                voice_probability=0,
                followup_meme_probability=0,
                random_meme_probability=1,
                random_value=lambda: 0,
            ),
        )
        plan = asyncio.run(planner.plan(ReplyDraft("今年分数线以官网为准"), event()))
        self.assertEqual(plan.channels, ("text",))

    def test_quote_target_is_locally_validated(self):
        item = MessageEvent(
            **{
                **event().__dict__, "platform_message_id": "88",
                "recent_message_ids": ("77", "88"),
            }
        )
        planner = DefaultReplyPlanner(
            quotes=QuotePolicy(probability=1.0, random_value=lambda: 0.0)
        )
        accepted = asyncio.run(planner.plan(ReplyDraft(
            "在说这个", conversation_decision={
                "target_message_id": "88", "relation": "answer"
            }
        ), item))
        rejected = asyncio.run(planner.plan(ReplyDraft(
            "不能乱引", conversation_decision={
                "target_message_id": "999999", "relation": "answer"
            }
        ), item))
        self.assertEqual(accepted.quote_message_id, "88")
        self.assertEqual(rejected.quote_message_id, "88")

        older = asyncio.run(planner.plan(ReplyDraft(
            "接前面那句", conversation_decision={
                "target_message_id": "77", "relation": "answer"
            }
        ), item))
        self.assertEqual(older.quote_message_id, "77")
    def test_native_qq_face_is_selected_locally_without_model_id(self):
        planner = DefaultReplyPlanner(
            qq_faces=QQFacePolicy(probability=1.0, random_value=lambda: 0.0)
        )
        plan = asyncio.run(planner.plan(ReplyDraft("已经完成了"), event()))
        self.assertEqual((plan.qq_face_name, plan.qq_face_id), ("胜利", "78"))

    def test_super_face_uses_napcat_supported_face_id(self):
        planner = DefaultReplyPlanner(
            qq_faces=QQFacePolicy(probability=1.0, random_value=lambda: 0.0)
        )
        plan = asyncio.run(planner.plan(ReplyDraft("给你鼓掌"), event()))
        self.assertEqual((plan.qq_face_name, plan.qq_face_id), ("超级鼓掌", "375"))

    def test_unavailable_media_falls_back_to_text(self):
        planner = DefaultReplyPlanner()
        plan = asyncio.run(
            planner.plan(ReplyDraft("你好", ("meme", "voice"), "不存在"), event())
        )
        self.assertEqual(plan.channels, ("text",))

    def test_only_exact_catalogue_tags_and_roster_ids_are_accepted(self):
        planner = DefaultReplyPlanner(
            ReplyCapabilities(frozenset({"开心"}), voice=True)
        )
        plan = asyncio.run(
            planner.plan(
                ReplyDraft("你好", ("meme",), "开心", at_user_id="10002"),
                event(),
            )
        )
        self.assertEqual((plan.channels, plan.at_user_id), (("meme",), "10002"))

        rejected = asyncio.run(
            planner.plan(
                ReplyDraft("你好", ("meme",), "开心", at_user_id="99999"),
                event(),
            )
        )
        self.assertEqual(rejected.at_user_id, "")

    def test_duplicate_text_is_suppressed_when_voice_is_selected(self):
        planner = DefaultReplyPlanner(ReplyCapabilities(voice=True))
        plan = asyncio.run(planner.plan(
            ReplyDraft("笑死了", ("text", "voice"), voice_text="笑死了！")
        ))
        self.assertEqual(plan.channels, ("voice",))

    def test_distinct_text_and_voice_may_be_combined(self):
        planner = DefaultReplyPlanner(ReplyCapabilities(voice=True))
        plan = asyncio.run(planner.plan(
            ReplyDraft("你先听我说", ("text", "voice"), voice_text="这件事真的有点离谱")
        ))
        self.assertEqual(plan.channels, ("text", "voice"))


class ReplyDispatchTests(unittest.TestCase):
    def meme_catalog_fixture(self, *, full_pack: bool = False) -> LocalMemeCatalog:
        """Exercise the shipped mapping without redistributing upstream images.

        The sender is a fake: these bytes test catalogue selection and base64
        transport, not an image decoder or NapCat's real rendering.
        """
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = Path(__file__).resolve().parents[1] / "memes" / "catalog.json"
        raw = json.loads(source.read_text(encoding="utf-8"))
        if full_pack:
            raw = {"packs": raw["packs"]}
            for pack in raw["packs"]:
                for category in pack["category_tags"]:
                    directory = root / pack["root"] / category
                    directory.mkdir(parents=True)
                    for name in ("one.png", "two.gif"):
                        (directory / name).write_bytes(b"fixture-image")
                    (directory / "README.txt").write_text("Not an image", encoding="utf-8")
        else:
            raw = {"memes": [
                item for item in raw["memes"]
                if item["file"].startswith("astrbot-official-01/")
            ]}
            for item in raw["memes"]:
                target = root / item["file"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"fixture-image")
        (root / "catalog.json").write_text(json.dumps(raw), encoding="utf-8")
        return LocalMemeCatalog(root)

    def test_curated_astrbot_pack_is_available_and_can_send_meme_only(self):
        catalog = self.meme_catalog_fixture()
        tags = {"疑惑", "震惊", "无语", "观望", "困了", "害羞", "调侃", "吃瓜", "开心", "卖萌"}
        self.assertLessEqual(len(str(catalog.available_tags())), 300)
        self.assertTrue(tags.issubset(set(catalog.available_tags())))
        for tag in tags:
            self.assertTrue(catalog.pick(tag).startswith("base64://"))
        sender = Sender()
        plan = asyncio.run(DefaultReplyPlanner(
            ReplyCapabilities(meme_tags=frozenset(tags))
        ).plan(ReplyDraft("这个表情就是我的回答", ("meme",), "吃瓜"), event()))
        result = asyncio.run(ReplyDispatcher(sender, ReplyMediaProcessor(catalog)).dispatch(
            plan, group_id="42", user_id=None,
        ))
        self.assertEqual(len(sender.sent), 1)
        self.assertIn("image", sender.sent[0])
        self.assertNotIn("text", sender.sent[0])
        self.assertTrue(result.sent_meme)

    def test_full_astrbot_pack_is_indexed_by_all_upstream_categories(self):
        catalog = self.meme_catalog_fixture(full_pack=True)
        upstream = catalog.root / "astrbot-official-01" / "upstream"
        files = {
            path.resolve() for path in upstream.glob("*/*")
            if path.is_file() and path.suffix in {".png", ".gif"}
        }
        indexed = {
            path for paths in catalog.entries.values() for path in paths
            if path.is_relative_to(upstream.resolve())
        }
        # Two images in each of the 19 categories; metadata must be ignored.
        self.assertEqual(len(files), 38)
        self.assertEqual(indexed, files)
        self.assertLessEqual(len(str(catalog.available_tags())), 300)

    def test_segmentation_prefers_sentence_over_later_comma(self):
        text = "这件事要先确认来源。" * 4 + "补充一点，别急着下结论，后面还要核对。" * 2
        parts = TextSegmenter(60).split(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].endswith("。"))
        self.assertEqual("".join(parts), text)

    def test_long_text_is_split_at_natural_boundaries(self):
        sender = Sender()
        dispatcher = ReplyDispatcher(sender, segmenter=TextSegmenter(60))
        text = "第一段说清楚这一件事。" * 4 + "第二段再补充另一件事。" * 4
        result = asyncio.run(dispatcher.dispatch(
            asyncio.run(DefaultReplyPlanner().plan(ReplyDraft(text))),
            group_id="42", user_id=None,
        ))
        self.assertGreater(len(sender.sent), 1)
        self.assertEqual("".join(item["text"] for item in sender.sent), text)
        self.assertTrue(result.sent_text)

    def test_quote_is_forwarded_as_structured_field(self):
        sender = Sender()
        plan = asyncio.run(DefaultReplyPlanner(
            quotes=QuotePolicy(probability=1.0, random_value=lambda: 0.0)
        ).plan(ReplyDraft("回答"), MessageEvent(
            **{**event().__dict__, "platform_message_id": "88"}
        )))
        asyncio.run(ReplyDispatcher(sender).dispatch(plan, group_id="42", user_id=None))
        self.assertEqual(sender.sent[-1]["reply_to"], "88")

    def test_humanized_pacer_is_an_injected_timing_policy(self):
        delays = []

        async def sleep(value): delays.append(value)

        pacer = HumanizedPacer(
            enabled=True, base_seconds=.2, per_character_seconds=.01,
            maximum_seconds=2, random_value=lambda: 0, sleep=sleep,
        )
        sender = Sender()
        asyncio.run(ReplyDispatcher(sender, pacer=pacer).dispatch(
            asyncio.run(DefaultReplyPlanner().plan(ReplyDraft("四个字啊"))),
            group_id="42", user_id=None,
        ))
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], .24)
    def test_native_qq_face_is_forwarded_as_structured_field(self):
        sender = Sender()
        planner = DefaultReplyPlanner(
            qq_faces=QQFacePolicy(probability=1.0, random_value=lambda: 0.0)
        )
        plan = asyncio.run(planner.plan(ReplyDraft("这个不错")))
        result = asyncio.run(ReplyDispatcher(sender).dispatch(plan, group_id="42", user_id=None))
        self.assertEqual(sender.sent[-1]["qq_face"], "76")
        self.assertTrue(result.sent_qq_face)

    def test_voice_only_does_not_duplicate_visible_text(self):
        sender, speech = Sender(), Speech()
        dispatcher = ReplyDispatcher(
            sender, ReplyMediaProcessor(Memes(), speech)
        )
        plan = asyncio.run(
            DefaultReplyPlanner(ReplyCapabilities(voice=True)).plan(
                ReplyDraft("语义文本", ("voice",), voice_text="请听这句")
            )
        )
        result = asyncio.run(
            dispatcher.dispatch(plan, group_id="42", user_id=None)
        )
        self.assertEqual(speech.seen, ["请听这句"])
        self.assertEqual(sender.sent, [{"group_id": "42", "user_id": None, "voice": "http://tts.test/reply.mp3"}])
        self.assertTrue(result.sent_voice)
        self.assertFalse(result.sent_text)

    def test_runtime_media_failure_falls_back_to_text(self):
        sender = Sender()
        dispatcher = ReplyDispatcher(
            sender, ReplyMediaProcessor(Memes(), Speech(source=""))
        )
        plan = asyncio.run(
            DefaultReplyPlanner(ReplyCapabilities(voice=True)).plan(
                ReplyDraft("保底文字", ("voice",))
            )
        )
        result = asyncio.run(
            dispatcher.dispatch(plan, group_id="42", user_id=None)
        )
        self.assertEqual(sender.sent[-1]["text"], "保底文字")
        self.assertTrue(result.sent_text)

    def test_text_meme_and_voice_are_delivered_without_another_model_call(self):
        sender, speech = Sender(), Speech()
        media = ReplyMediaProcessor(Memes({"开心": "base64://Z2lm"}), speech)
        dispatcher = ReplyDispatcher(sender, media)
        planner = DefaultReplyPlanner(
            ReplyCapabilities(frozenset({"开心"}), voice=True)
        )
        plan = asyncio.run(planner.plan(ReplyDraft(
            "你看这个", ("text", "meme", "voice"), "开心",
            voice_text="这下真的好耶",
        )))
        result = asyncio.run(dispatcher.dispatch(plan, group_id="42", user_id=None))
        self.assertEqual(len(sender.sent), 3)
        self.assertEqual(sender.sent[0]["text"], "你看这个")
        self.assertEqual(sender.sent[1]["image"], "base64://Z2lm")
        self.assertNotIn("image", sender.sent[0])
        self.assertIn("voice", sender.sent[2])
        self.assertEqual(speech.seen, ["这下真的好耶"])
        self.assertTrue(result.sent_text and result.sent_meme and result.sent_voice)


if __name__ == "__main__":
    unittest.main()
