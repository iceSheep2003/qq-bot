from __future__ import annotations

import asyncio
import unittest
from datetime import date

from qunbot.adapters.onebot import OneBotError, OneBotGateway
from qunbot.domain import MessageEvent
from qunbot.social_interactions import (
    LightInteractionPolicy, LightInteractionSettings, PokeController, PokeSettings,
    ReactionPolicy, ReactionSettings,
)


def event(mid: str, text: str = "今天终于学完计网了", *, at_bot: bool = False):
    return MessageEvent(
        event_id=f"bot:{mid}", scope="group:42", group_id="42",
        user_id="7", nickname="小明", text=text, image_urls=(),
        at_bot=at_bot, at_users=(), timestamp=1,
        platform_message_id=mid,
    )


class ReactionPolicyTests(unittest.TestCase):
    def policy(self, **overrides):
        settings = ReactionSettings(
            enabled=True, probability=1.0, cooldown_seconds=0,
            daily_group_limit=80, daily_user_limit=20,
            **overrides,
        )
        return ReactionPolicy(
            settings,
            random_value=lambda: 0.0,
            choose=lambda values: values[0],
            clock=lambda: 100.0,
            today=lambda: date(2026, 9, 25),
        )

    def test_clear_social_cue_selects_owner_controlled_reaction(self):
        decision = self.policy().decide(event("101"))
        self.assertTrue(decision.react)
        self.assertEqual((decision.name, decision.emoji_id), ("胜利", "78"))

    def test_plain_message_does_not_get_a_random_reaction(self):
        decision = self.policy().decide(event("102", "我准备去图书馆"))
        self.assertFalse(decision.react)

    def test_same_event_is_never_reacted_to_twice(self):
        policy = self.policy()
        self.assertTrue(policy.decide(event("103")).react)
        self.assertFalse(policy.decide(event("103")).react)

    def test_sensitive_message_is_not_decorated(self):
        self.assertFalse(self.policy().decide(event("104", "验证码是666666")).react)

    def test_direct_mentions_use_the_lower_probability(self):
        policy = ReactionPolicy(
            ReactionSettings(probability=1.0, cooldown_seconds=0),
            random_value=lambda: 0.8,
        )
        self.assertFalse(policy.decide(event("105", at_bot=True)).react)


class OneBotReactionAdapterTests(unittest.TestCase):
    def test_reaction_maps_to_napcat_action(self):
        gateway = OneBotGateway("127.0.0.1", 6199, "token")
        calls = []

        async def call(action, params, **_kwargs):
            calls.append((action, params))
            return {}

        gateway.call = call
        asyncio.run(gateway.react_to_message("123", "76"))
        self.assertEqual(
            calls,
            [("set_msg_emoji_like", {"message_id": 123, "emoji_id": "76"})],
        )

    def test_untrusted_ids_are_rejected_before_napcat(self):
        gateway = OneBotGateway("127.0.0.1", 6199, "token")
        with self.assertRaises(OneBotError):
            asyncio.run(gateway.react_to_message("not-an-id", "76"))
        with self.assertRaises(OneBotError):
            asyncio.run(gateway.react_to_message("123", "smile"))

    def test_native_dice_and_group_poke_are_structured_actions(self):
        gateway = OneBotGateway("127.0.0.1", 6199, "token")
        calls = []

        async def call(action, params, **_kwargs):
            calls.append((action, params)); return {}

        gateway.call = call
        asyncio.run(gateway.send_native("42", "dice"))
        asyncio.run(gateway.poke_group("42", "7"))
        self.assertEqual(calls[0][1]["message"][0]["type"], "dice")
        self.assertEqual(calls[1], ("group_poke", {"group_id": 42, "user_id": 7}))

    def test_bounded_quote_chain_is_resolved(self):
        gateway = OneBotGateway("127.0.0.1", 6199, "token")

        async def call(_action, params, **_kwargs):
            if params["message_id"] == 1:
                return {"message": [
                    {"type": "text", "data": {"text": "新一层"}},
                    {"type": "reply", "data": {"id": "2"}},
                    {"type": "image", "data": {"url": "https://img/1"}},
                ]}
            return {"message": [{"type": "text", "data": {"text": "旧一层"}}]}

        gateway.call = call
        result = asyncio.run(gateway.resolve_quote("1"))
        self.assertEqual(result["text"], "新一层\n↳ 旧一层")
        self.assertEqual(result["image_urls"], ("https://img/1",))


class LightweightInteractionTests(unittest.TestCase):
    def test_plus_one_lets_bot_join_without_model(self):
        policy = LightInteractionPolicy(
            LightInteractionSettings(1.0, 0, 20), random_value=lambda: 0.0,
            clock=lambda: 10,
        )
        rows = [
            {"role": "user", "user_id": "1", "content": "今晚早睡"},
            {"role": "user", "user_id": "2", "content": "+1"},
        ]
        decision = policy.decide(event("201", "+1"), rows)
        self.assertEqual((decision.kind, decision.text), ("repeat", "今晚早睡"))

    def test_media_placeholder_is_never_repeated_as_text(self):
        policy = LightInteractionPolicy(
            LightInteractionSettings(1.0, 0, 20), random_value=lambda: 0.0,
            clock=lambda: 10,
        )
        for placeholder in ("[图片]", "[语音]", "[表情]", "[文件]"):
            rows = [
                {"role": "user", "user_id": "1", "content": placeholder},
                {"role": "user", "user_id": "2", "content": placeholder},
            ]
            self.assertFalse(policy.decide(event("201", placeholder), rows).active)

    def test_direct_dice_request_uses_native_action(self):
        decision = LightInteractionPolicy().decide(
            event("202", "掷骰子", at_bot=True), []
        )
        self.assertEqual(decision.kind, "dice")


class _PokeService:
    def __init__(self): self.ingested, self.recorded = [], []
    async def ingest(self, item): self.ingested.append(item); return True
    def record_assistant_turn(self, event, text, *, event_id):
        self.recorded.append((text, event_id))
    async def send_reply(self, group, user, text, event=None):
        return text


class _PokeSender:
    def __init__(self): self.pokes, self.sent = [], []
    async def poke_group(self, group, user): self.pokes.append((group, user))
    async def send(self, **kwargs): self.sent.append(kwargs)


class PokeControllerTests(unittest.TestCase):
    def test_poke_can_counterpoke_without_canned_text(self):
        service, sender = _PokeService(), _PokeSender()
        controller = PokeController(
            PokeSettings(counter_probability=1),
            service, sender, frozenset({"42"}), random_value=lambda: 0,
            clock=lambda: 100,
        )
        consumed = asyncio.run(controller.handle({
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "group_id": 42, "user_id": 7, "target_id": 99, "self_id": 99,
            "time": 10,
        }))
        self.assertTrue(consumed)
        self.assertEqual(sender.pokes, [("42", "7")])
        self.assertEqual(sender.sent, [])
        self.assertEqual(len(service.recorded), 1)

    def test_bot_follows_when_several_people_poke_same_member(self):
        service, sender = _PokeService(), _PokeSender()
        controller = PokeController(
            PokeSettings(
                counter_probability=0,
                follow_distinct_users=3, follow_probability=1,
                follow_cooldown_seconds=180,
            ),
            service, sender, frozenset({"42"}), random_value=lambda: 0,
            clock=lambda: 100,
        )
        for actor in ("1", "2", "3"):
            consumed = asyncio.run(controller.handle({
                "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
                "group_id": 42, "user_id": actor, "target_id": 8, "self_id": 99,
            }))
            self.assertTrue(consumed)
        self.assertEqual(sender.pokes, [("42", "8")])
        self.assertEqual(service.ingested, [])


if __name__ == "__main__":
    unittest.main()
