import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from qunbot.infrastructure.adapters.events import parse_message
from qunbot.infrastructure.adapters.inbound_media import (
    MAX_BYTES,
    NapCatMediaSource,
    _MediaRequestFilter,
)
from qunbot.infrastructure.adapters.onebot import OneBotGateway, OneBotError
from qunbot.infrastructure.adapters.perception import configured_perception
from qunbot.contracts.schema import ContractError
from qunbot.agent.conversation.projection import ConversationProjector
from qunbot.contracts.models import InboundPart
from qunbot.contracts.ontology.objects import stable_id
from qunbot.agent.perception.inbound import InboundMedia
from qunbot.agent.orchestrator import Agent
from qunbot.agent.service import BotPolicy, ConversationService
from qunbot.infrastructure.storage.assets import AssetStore
from qunbot.infrastructure.storage.conversation import ConversationStore
from qunbot.infrastructure.storage.database import SqliteDatabase
from qunbot.infrastructure.storage.ontology import GraphStore


class InboundMediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SqliteDatabase(Path(self.tmp.name) / "test.db")
        self.graph = GraphStore(self.db)
        self.ledger = ConversationStore(self.db)
        self.assets = AssetStore(self.db, Path(self.tmp.name) / "assets", self.graph)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def event(self, message_id=1, parts=None):
        return parse_message(
            {
                "post_type": "message",
                "message_type": "group",
                "group_id": 42,
                "user_id": 7,
                "self_id": 9,
                "message_id": message_id,
                "time": 1,
                "message": parts
                or [
                    {
                        "type": "image",
                        "data": {
                            "url": "https://gchat.qpic.cn/image?secret=qqsignature"
                        },
                    }
                ],
            }
        )

    async def test_media_bytes_references_quote_and_membership_reach_graph(self):
        payload = b"GIF89a" + b"synthetic"
        source = SimpleNamespace(acquire=AsyncMock(return_value=payload))
        event = self.event()
        self.ledger.add_event(event)
        prepared = await InboundMedia(source, self.assets, self.ledger).prepare(event)
        self.assertEqual(prepared.ledger_parts[0]["kind"], "image")
        self.assertEqual(prepared.image_urls, ())
        self.assertFalse(self.ledger.add_event(prepared))
        projector = ConversationProjector(self.ledger, self.graph)
        self.assertEqual(projector.process(), 1)
        message_id = stable_id("message", event.scope, event.event_id)
        self.assertEqual(
            len(self.graph.outgoing(event.scope, message_id, "contains_media")), 1
        )
        self.assertEqual(len(self.graph.list_nodes(event.scope, "Membership")), 1)
        quoted = self.event(
            2,
            [
                {"type": "reply", "data": {"id": "1"}},
                {"type": "text", "data": {"text": "这个图"}},
                {"type": "face", "data": {"id": "14"}},
            ],
        )
        self.ledger.add_event(quoted)
        projector.process()
        reply = self.graph.get(
            quoted.scope, stable_id("message", quoted.scope, quoted.event_id)
        )
        self.assertEqual(reply["reply_to_ref"]["id"], message_id)
        self.assertEqual(reply["parts"][-1], {"kind": "face", "face_id": "14"})
        with self.assertRaises(ContractError):
            self.assets.resolve(
                "group:another", prepared.ledger_parts[0]["asset_ref"]["id"]
            )

    async def test_live_path_acquires_only_admitted_nonduplicate_events(self):
        source = SimpleNamespace(acquire=AsyncMock(return_value=b"GIF89asynthetic"))
        people = SimpleNamespace(
            observe_user=lambda *a: None, observe_group_member=lambda *a: None
        )
        policy = BotPolicy(frozenset({"42"}), 100, "Asia/Shanghai", False)
        service = ConversationService(
            None,
            self.ledger,
            people,
            None,
            None,
            policy,
            inbound_media=InboundMedia(source, self.assets, self.ledger),
            restore_last_reply=False,
        )
        service.decide_reply = AsyncMock(return_value=True)
        service.execute_turn = AsyncMock()
        from dataclasses import replace

        event = self.event()
        await service.handle_message(replace(event, group_id="99", scope="group:99"))
        source.acquire.assert_not_awaited()
        await service.handle_message(event)
        await service.handle_message(event)
        source.acquire.assert_awaited_once()
        service.execute_turn.assert_awaited_once()
        delivered_event = service.execute_turn.await_args.args[0]
        self.assertTrue(delivered_event.has_image)
        self.assertEqual(delivered_event.ledger_parts[0]["kind"], "image")

    async def test_acquisition_failure_does_not_invent_content(self):
        event = self.event(parts=[{"type": "record", "data": {"file": "voice.silk"}}])
        self.ledger.add_event(event)
        source = SimpleNamespace(acquire=AsyncMock(return_value=b"#!SILK_V3"))
        prepared = await InboundMedia(source, self.assets, self.ledger).prepare(event)
        self.assertEqual(prepared.ledger_parts[0]["kind"], "unknown")
        self.assertEqual(self.ledger.actual_recent(event.scope)[0]["content"], "[语音]")
        self.assertNotIn(
            "voice.silk", self.ledger.actual_recent(event.scope)[0]["parts_json"]
        )

    async def test_primary_chat_is_text_only_and_prefix_is_media_independent(self):
        event = self.event()
        skills = SimpleNamespace(catalog_text=lambda: "", select=lambda *a, **k: [])
        agent = Agent(
            None,
            self.ledger,
            SimpleNamespace(profile=lambda *a: {}),
            SimpleNamespace(related=lambda *a: []),
            skills,
            None,
            Path(__file__).parents[2] / "config/persona.md",
        )
        messages = agent.build_messages(event)
        self.assertTrue(all(isinstance(m["content"], str) for m in messages))
        self.assertNotIn("qqsignature", json.dumps(messages))
        self.assertIn("vision_analyze", messages[-1]["content"])
        self.assertEqual(
            agent.stable_prefix(), agent.build_messages(self.event(2))[0]["content"]
        )

    async def test_local_mount_and_record_conversion_are_explicit(self):
        root = Path(self.tmp.name) / "napcat"
        root.mkdir()
        audio = root / "voice.wav"
        audio.write_bytes(b"RIFF0000WAVEtest")
        gateway = SimpleNamespace(call=AsyncMock(return_value={"file": str(audio)}))
        source = NapCatMediaSource(gateway, roots=[root])
        try:
            self.assertEqual(
                await source.acquire(InboundPart("audio", file_ref="voice.silk")),
                audio.read_bytes(),
            )
            gateway.call.assert_awaited_once_with(
                "get_record", {"file": "voice.silk", "out_format": "wav"}
            )
            with self.assertRaises(ContractError):
                source._read_local(Path(__file__))
        finally:
            await source.close()

    async def test_https_dns_pinned_no_redirects_or_unbounded_payload(self):
        requests = []

        def serve(request):
            requests.append(request)
            return httpx.Response(200, content=b"GIF89asynthetic")

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(serve), follow_redirects=False
        )
        source = NapCatMediaSource(None, client=client)
        dns = AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443))])
        try:
            with patch.object(
                __import__("asyncio").get_running_loop(), "getaddrinfo", dns
            ):
                payload = await source._download(
                    "https://gchat.qpic.cn/image?signature=secret"
                )
            self.assertTrue(payload.startswith(b"GIF89a"))
            self.assertEqual(requests[0].url.host, "8.8.8.8")
            self.assertEqual(requests[0].headers["Host"], "gchat.qpic.cn")
            self.assertEqual(requests[0].extensions["sni_hostname"], "gchat.qpic.cn")
            for denied in [
                "http://gchat.qpic.cn/a",
                "https://127.0.0.1/a",
                "https://gchat.qpic.cn.evil/a",
                "https://u:p@gchat.qpic.cn/a",
            ]:
                with self.assertRaises(ContractError):
                    await source._download(denied)
            dns.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
            with patch.object(
                __import__("asyncio").get_running_loop(), "getaddrinfo", dns
            ):
                with self.assertRaises(ContractError):
                    await source._download("https://gchat.qpic.cn/a")
        finally:
            await source.close()

    async def test_redirect_and_oversize_fail_closed(self):
        for response in [
            httpx.Response(302, headers={"location": "https://127.0.0.1"}),
            httpx.Response(
                200, headers={"content-length": str(MAX_BYTES + 1)}, content=b"x"
            ),
            httpx.Response(200, content=b"x" * (MAX_BYTES + 1)),
        ]:
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response),
                follow_redirects=False,
            )
            source = NapCatMediaSource(None, client=client)
            dns = AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443))])
            try:
                with patch.object(
                    __import__("asyncio").get_running_loop(), "getaddrinfo", dns
                ):
                    with self.assertRaises((ContractError, ConnectionError)):
                        await source._download("https://gchat.qpic.cn/a")
            finally:
                await source.close()

    def test_signed_request_queries_are_not_logged(self):
        record = logging.LogRecord(
            "httpx",
            logging.INFO,
            "",
            0,
            "Request %s",
            (httpx.URL("https://gchat.qpic.cn/a?signature=secret"),),
            None,
        )
        _MediaRequestFilter().filter(record)
        self.assertNotIn("secret", record.getMessage())

    async def test_quote_from_another_group_is_rejected(self):
        gateway = OneBotGateway("127.0.0.1", 0, "test")
        gateway.call = AsyncMock(
            return_value={
                "group_id": 99,
                "message": [{"type": "text", "data": {"text": "other group"}}],
            }
        )
        with self.assertRaises(OneBotError):
            await gateway.resolve_quote("1", group_id="42")

    def test_all_provider_configurations_validated_before_client_creation(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "BOT_VISION_BASE_URL": "https://vision.example/v1",
                    "BOT_VISION_API_KEY": "test",
                    "BOT_VISION_MODEL": "test",
                    "BOT_ASR_BASE_URL": "https://asr.example/v1",
                    "BOT_ASR_API_KEY": "",
                    "BOT_ASR_MODEL": "",
                },
            ),
            patch("qunbot.infrastructure.adapters.perception.ModelClient") as client,
        ):
            with self.assertRaises(ValueError):
                configured_perception()
            client.assert_not_called()
