from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from qunbot.domain import MessageEvent
from qunbot.extensions.qq_admin.config import QQAdminConfig
from qunbot.extensions.qq_admin.service import QQAdminService
from qunbot.runtime.tools import Tool, ToolRegistry


def event(*, user="7", message="100", reply="") -> MessageEvent:
    return MessageEvent(
        event_id="e1", scope="group:42", group_id="42", user_id=user,
        nickname="测试员", text="管理请求", image_urls=(), at_bot=True,
        at_users=(), timestamp=1, platform_message_id=message,
        reply_to_message_id=reply,
    )


class Gateway:
    def __init__(self):
        self.calls = []
        self.sent = []

    async def call(self, action, params):
        self.calls.append((action, params))
        if action == "get_group_member_info":
            return {"role": "member", "card": "新同学"}
        if action == "get_group_root_files":
            return {
                "files": [{"file_name": "群规.pdf", "file_id": "a", "file_size": 3}],
                "folders": [{"folder_name": "资料", "folder_id": "f1"}],
            }
        if action == "get_group_files_by_folder":
            return {"files": [{"file_name": "讲义.zip", "file_id": "b", "file_size": 9}]}
        return {}

    async def send(self, *, group_id, user_id, text, **kwargs):
        self.sent.append((group_id, user_id, text, kwargs))
        return {"message_id": "proposal-1"}


class Agent:
    async def reply(self, _event, proactive=False):
        raise RuntimeError("model unavailable")


class QQAdminTests(unittest.IsolatedAsyncioTestCase):
    def service(self, **overrides):
        fields = dict(
            operator_ids=frozenset({"7"}), max_mute_seconds=600,
            join_review_enabled=True, join_approve_keywords=("答案42",),
            join_reject_keywords=("广告",), join_default_approve=False,
            welcome_enabled=True,
        )
        fields.update(overrides)
        service = QQAdminService(QQAdminConfig(**fields))
        gateway = Gateway()
        service.bind(SimpleNamespace(
            sender=gateway,
            policy=SimpleNamespace(allowed_groups=frozenset({"42"})),
            agent=Agent(),
        ))
        return service, gateway

    async def test_async_tool_registry_awaits_handlers(self):
        registry = ToolRegistry()

        async def handler(args, _event):
            return "ok:" + args["value"]

        registry.register(Tool(
            name="async", description="", parameters={"type": "object"},
            handler=handler,
        ))
        self.assertEqual(await registry.acall("async", {"value": "1"}, event()), "ok:1")

    async def test_only_local_operator_can_mute(self):
        service, gateway = self.service()
        with self.assertRaisesRegex(ValueError, "locally configured operator"):
            await service.mute_member({"user_id": "8", "duration": 60}, event(user="9"))
        self.assertEqual(gateway.calls, [])

    async def test_mute_checks_role_then_calls_onebot(self):
        service, gateway = self.service()
        answer = await service.mute_member({"user_id": "8", "duration": 60}, event())
        self.assertEqual(answer, "已禁言 60 秒")
        self.assertEqual([name for name, _ in gateway.calls], [
            "get_group_member_info", "set_group_ban",
        ])

    async def test_essence_is_limited_to_current_or_quoted_message(self):
        service, gateway = self.service()
        with self.assertRaisesRegex(ValueError, "current or explicitly replied"):
            await service.set_essence({"message_id": "999", "enable": True}, event(reply="88"))
        await service.set_essence({"message_id": "88", "enable": True}, event(reply="88"))
        self.assertEqual(gateway.calls[-1], ("set_essence_msg", {"message_id": 88}))

    async def test_group_file_search_is_bounded_and_includes_folders(self):
        service, gateway = self.service()
        rows = json.loads(await service.list_group_files({}, event(user="99")))
        self.assertEqual([row["name"] for row in rows], ["群规.pdf", "讲义.zip"])
        self.assertEqual(rows[1]["folder"], "资料")
        self.assertEqual([name for name, _ in gateway.calls], [
            "get_group_root_files", "get_group_files_by_folder",
        ])

    async def test_join_review_is_rules_first_and_unknown_stays_pending(self):
        service, gateway = self.service()
        base = {"post_type": "request", "request_type": "group", "group_id": 42,
                "user_id": 8, "flag": "x", "sub_type": "add"}
        await service.handle_raw({**base, "comment": "答案42"})
        self.assertEqual(gateway.calls[-1][1]["approve"], True)
        count = len(gateway.calls)
        await service.handle_raw({**base, "comment": "没写验证答案"})
        self.assertEqual(len(gateway.calls), count)
        await service.handle_raw({**base, "comment": "发广告"})
        self.assertEqual(gateway.calls[-1][1]["approve"], False)

    async def test_welcome_stays_silent_without_model(self):
        service, gateway = self.service()
        raw = {"post_type": "notice", "notice_type": "group_increase",
               "group_id": 42, "user_id": 8, "self_id": 1}
        self.assertTrue(await service.handle_raw(raw))
        self.assertEqual(gateway.sent, [])

    async def test_title_target_can_consent_to_exact_proposal_reply(self):
        service, gateway = self.service()
        answer = await service.propose_title(
            {"user_id": "8", "title": "资料侠", "reason": "经常分享资料"}, event()
        )
        self.assertIn("等待本人同意", answer)
        raw = {
            "post_type": "message", "message_type": "group", "group_id": 42,
            "user_id": 8, "self_id": 1,
            "message": [
                {"type": "reply", "data": {"id": "proposal-1"}},
                {"type": "text", "data": {"text": "同意头衔"}},
            ],
        }
        self.assertTrue(await service.handle_raw(raw))
        self.assertIn("set_group_special_title", [name for name, _ in gateway.calls])
        params = next(params for name, params in gateway.calls if name == "set_group_special_title")
        self.assertEqual(params["special_title"], "资料侠")

    async def test_title_votes_are_unique_and_must_reply_to_proposal(self):
        service, gateway = self.service(title_vote_threshold=2)
        await service.propose_title(
            {"user_id": "8", "title": "气氛组", "reason": "经常活跃气氛"}, event()
        )
        def vote(user, reply="proposal-1"):
            return {
                "post_type": "message", "message_type": "group", "group_id": 42,
                "user_id": user, "self_id": 1,
                "message": [
                    {"type": "reply", "data": {"id": reply}},
                    {"type": "text", "data": {"text": "赞成头衔"}},
                ],
            }
        self.assertFalse(await service.handle_raw(vote(9, "other-message")))
        self.assertTrue(await service.handle_raw(vote(9)))
        self.assertTrue(await service.handle_raw(vote(9)))
        self.assertNotIn("set_group_special_title", [name for name, _ in gateway.calls])
        self.assertTrue(await service.handle_raw(vote(10)))
        self.assertIn("set_group_special_title", [name for name, _ in gateway.calls])

    async def test_title_target_can_veto(self):
        service, gateway = self.service()
        await service.propose_title(
            {"user_id": "8", "title": "早起王", "reason": "每天早起"}, event()
        )
        raw = {
            "post_type": "message", "message_type": "group", "group_id": 42,
            "user_id": 8, "self_id": 1,
            "message": [
                {"type": "reply", "data": {"id": "proposal-1"}},
                {"type": "text", "data": {"text": "拒绝头衔"}},
            ],
        }
        self.assertTrue(await service.handle_raw(raw))
        self.assertNotIn("set_group_special_title", [name for name, _ in gateway.calls])


if __name__ == "__main__":
    unittest.main()
