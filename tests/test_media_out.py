"""Structured outbound media: bounded marker parsing, part resolution, @ safety.

No socket is opened and no real group is touched. The OneBot gateway is driven
through a fake connection, exactly like ``test_onebot.py``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import unittest
from unittest import mock

from qunbot.adapters.onebot import OneBotGateway
from qunbot.domain import MessageEvent
from qunbot.extensions.media import ReplyMediaProcessor
from qunbot.extensions.media_parts import (
    At,
    Image,
    MediaUrlPolicy,
    OutboundMessage,
    Text,
    Voice,
    max_image_bytes,
    max_url_chars,
    parse_outbound,
    roster_from_event,
    strip_markers,
    valid_media_url,
    validate_at_target,
)

_SENTINEL = object()


class _FakeConnection:
    """Minimal stand-in for a websockets ServerConnection."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is _SENTINEL:
            raise StopAsyncIteration
        return item

    async def feed(self, frame: object) -> None:
        await self.queue.put(frame)

    async def close(self) -> None:
        await self.queue.put(_SENTINEL)

    async def send(self, data: str) -> None:
        self.sent.append(data)


async def _settle(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was never met")
        await asyncio.sleep(0)


class _Catalog:
    def __init__(self, source: str | None = "base64://Z2lm"):
        self.calls: list[str] = []
        self.source = source

    def pick(self, tag: str) -> str | None:
        self.calls.append(tag)
        return self.source


class _ExplodingCatalog:
    def pick(self, tag: str) -> str | None:
        raise OSError("catalog disk vanished")


class _Speech:
    def __init__(self, result: str | None = "http://tts.test/a.mp3", error=None):
        self.calls: list[str] = []
        self.result = result
        self.error = error

    async def synthesize(self, text: str) -> str:
        self.calls.append(text)
        if self.error is not None:
            raise self.error
        return self.result or ""


def _event(*, user_id="7", at_users=()):
    return MessageEvent(
        event_id="1:1",
        scope="group:42",
        group_id="42",
        user_id=user_id,
        nickname="小明",
        text="",
        image_urls=(),
        at_bot=False,
        at_users=tuple(at_users),
        timestamp=0,
    )


class NonAsciiTagTests(unittest.TestCase):
    """Tags are whatever the deployer wrote in the catalogue.

    Regression: the tag charset was ``[A-Za-z0-9_-]``, so every Chinese tag —
    which is what a Chinese group's catalogue naturally holds — failed to match
    and the marker survived as literal text. The suite missed it because every
    fixture used an ASCII tag like ``happy``; the shape of the test data hid
    the shape of the production data.
    """

    def test_a_chinese_tag_parses(self):
        message = parse_outbound("加油 [[meme:加油]]")
        self.assertEqual(message.text, "加油")
        image = next(p for p in message.parts if isinstance(p, Image))
        self.assertEqual(image.ref, "加油")

    def test_a_tag_mixing_scripts_parses(self):
        message = parse_outbound("走一个 [[meme:打call]]")
        self.assertEqual(message.text, "走一个")
        self.assertEqual(
            next(p for p in message.parts if isinstance(p, Image)).ref, "打call"
        )

    def test_a_chinese_tag_resolves_against_the_catalog(self):
        class Catalog:
            def pick(self, tag):
                return f"base64://{tag}" if tag == "加油" else None

        processor = ReplyMediaProcessor(Catalog())
        message = asyncio.run(
            processor.compose_message("加油 [[meme:加油]]", allowed_at=frozenset())
        )
        self.assertEqual(message.text, "加油")
        self.assertEqual(message.image, "base64://加油")

    def test_a_tag_cannot_escape_the_marker(self):
        # A bracket inside the tag would close the marker early and let the
        # remainder of the reply become syntax the deployer never defined.
        text = "你好 [[meme:a]]b]]"
        self.assertEqual(parse_outbound(text).text, text)

    def test_a_tag_cannot_contain_whitespace(self):
        text = "你好 [[meme:a b]]"
        self.assertEqual(parse_outbound(text).text, text)


class ParseTests(unittest.TestCase):
    def test_trailing_markers_become_parts(self):
        message = parse_outbound("给你看看 [[meme:happy]] [[voice]]")
        self.assertEqual(message.text, "给你看看")
        self.assertEqual([type(p) for p in message.parts], [Text, Image, Voice])
        # Nothing is resolved at parse time: source stays empty.
        self.assertIsNone(message.image)
        self.assertIsNone(message.voice)
        image = next(p for p in message.parts if isinstance(p, Image))
        self.assertEqual((image.ref, image.source), ("happy", ""))
        voice = next(p for p in message.parts if isinstance(p, Voice))
        self.assertEqual((voice.ref, voice.source, voice.text), (True, "", "给你看看"))

    def test_marker_in_the_middle_is_literal(self):
        text = "你好 [[meme:happy]] 中间还有话"
        message = parse_outbound(text)
        self.assertEqual(message.text, text)
        self.assertEqual(message.parts, (Text(text),))

    def test_text_after_a_marker_makes_it_literal(self):
        text = "[[voice]] 前"
        self.assertEqual(parse_outbound(text).text, text)

    def test_a_repeated_marker_is_not_consumed(self):
        text = "你好 [[meme:a]] [[meme:b]]"
        message = parse_outbound(text)
        self.assertEqual(message.text, text)
        self.assertNotIn("meme", {type(p).__name__ for p in message.parts})

    def test_a_repeated_voice_marker_is_not_consumed(self):
        text = "你好 [[voice]] [[voice]]"
        self.assertEqual(parse_outbound(text).text, text)

    def test_malformed_marker_is_literal(self):
        for text in ("你好 [[meme:]]", "你好 [[voice]", "你好 [[meme:a b]]", "你好 [[nope]]"):
            with self.subTest(text=text):
                self.assertEqual(parse_outbound(text).text, text)

    def test_plain_text_is_untouched(self):
        self.assertEqual(parse_outbound("普通的一条回复").text, "普通的一条回复")

    def test_empty_text_has_no_parts(self):
        self.assertEqual(parse_outbound("").parts, ())
        self.assertEqual(parse_outbound("   ").parts, ())

    def test_user_writing_marker_chars_is_preserved(self):
        text = "我也想发 [[meme:happy]] 这种格式"
        self.assertEqual(parse_outbound(text).text, text)

    def test_strip_markers_removes_every_well_formed_marker(self):
        self.assertEqual(
            strip_markers("你好 [[meme:happy]] 中 [[voice]] 尾 [[at:10001]]"), "你好  中  尾"
        )


class AtTargetTests(unittest.TestCase):
    def test_at_marker_needs_a_roster(self):
        text = "你好 [[at:10001]]"
        self.assertEqual(parse_outbound(text).text, text, "no roster means no @")

    def test_at_marker_outside_the_roster_is_literal(self):
        text = "你好 [[at:99999]]"
        message = parse_outbound(text, allowed_at={"10001"})
        self.assertEqual(message.text, text)
        self.assertEqual(message.parts, (Text(text),))

    def test_at_marker_inside_the_roster_is_a_part(self):
        message = parse_outbound("你好 [[at:10001]]", allowed_at={"10001"})
        self.assertEqual(message.text, "你好")
        self.assertEqual(message.at_user, "10001")

    def test_roster_from_event_includes_sender_and_mentioned(self):
        roster = roster_from_event(_event(user_id="7", at_users=("10001", "not-a-qq")))
        self.assertEqual(roster, frozenset({"7", "10001"}))

    def test_validate_at_target_rejects_non_qq_shapes(self):
        self.assertIsNone(validate_at_target("", {"7"}))
        self.assertIsNone(validate_at_target("abc", {"7"}))
        self.assertIsNone(validate_at_target("7; rm -rf /", {"7"}))
        self.assertIsNone(validate_at_target("7", None))
        self.assertEqual(validate_at_target("10001", {"10001"}), "10001")

    def test_at_marker_cannot_inject_an_onebot_command(self):
        payload = '你好 [[at:12345[face]]'
        self.assertEqual(parse_outbound(payload, allowed_at={"12345"}).text, payload)


class ProcessorTests(unittest.TestCase):
    def test_legacy_compose_signature_still_returns_a_triple(self):
        processor = ReplyMediaProcessor(_Catalog())
        result = asyncio.run(processor.compose("你好 [[meme:happy]] [[voice]]"))
        self.assertEqual(result, ("你好", "base64://Z2lm", None))

    def test_compose_message_exposes_structured_parts(self):
        processor = ReplyMediaProcessor(_Catalog(), _Speech())
        message = asyncio.run(
            processor.compose_message("你好 [[meme:happy]] [[voice]]")
        )
        self.assertEqual(message.text, "你好")
        self.assertEqual(message.image, "base64://Z2lm")
        self.assertEqual(message.voice, "http://tts.test/a.mp3")
        self.assertEqual(
            message.send_kwargs(),
            {
                "text": "你好",
                "image": "base64://Z2lm",
                "voice": "http://tts.test/a.mp3",
            },
        )

    def test_tts_failure_falls_back_to_text(self):
        speech = _Speech(error=RuntimeError("tts down"))
        processor = ReplyMediaProcessor(_Catalog(), speech)
        message = asyncio.run(processor.compose_message("你好 [[voice]]"))
        self.assertEqual(message.text, "你好")
        self.assertIsNone(message.voice)
        self.assertEqual(speech.calls, ["你好"])

    def test_disabled_voice_creates_no_client_and_never_synthesizes(self):
        # ``speech`` is None exactly when the voice extension was not enabled.
        processor = ReplyMediaProcessor(_Catalog(), None)
        message = asyncio.run(processor.compose_message("你好 [[voice]]"))
        self.assertEqual(message.text, "你好")
        self.assertIsNone(message.voice)

    def test_tts_returning_nothing_is_a_text_fallback(self):
        processor = ReplyMediaProcessor(_Catalog(), _Speech(result=None))
        message = asyncio.run(processor.compose_message("你好 [[voice]]"))
        self.assertIsNone(message.voice)
        self.assertEqual(message.text, "你好")

    def test_an_oversized_image_is_dropped_without_failing(self):
        payload = base64.b64encode(b"x" * 4096).decode()
        processor = ReplyMediaProcessor(
            _Catalog("base64://" + payload), image_bytes_limit=128
        )
        message = asyncio.run(processor.compose_message("看图 [[meme:big]]"))
        self.assertEqual(message.text, "看图")
        self.assertIsNone(message.image)

    def test_a_broken_catalog_does_not_break_the_turn(self):
        processor = ReplyMediaProcessor(_ExplodingCatalog())
        message = asyncio.run(processor.compose_message("看图 [[meme:happy]]"))
        self.assertEqual(message.text, "看图")
        self.assertIsNone(message.image)

    def test_a_non_http_tts_result_is_dropped(self):
        processor = ReplyMediaProcessor(_Catalog(), _Speech(result="file:///etc/passwd"))
        message = asyncio.run(processor.compose_message("你好 [[voice]]"))
        self.assertIsNone(message.voice)
        self.assertEqual(message.text, "你好")

    def test_at_target_is_validated_against_the_roster_in_the_processor(self):
        event = _event(user_id="7", at_users=("10001",))
        processor = ReplyMediaProcessor(_Catalog())
        ok = asyncio.run(
            processor.compose_message("你好 [[at:10001]]", allowed_at=roster_from_event(event))
        )
        rejected = asyncio.run(
            processor.compose_message("你好 [[at:99999]]", allowed_at=roster_from_event(event))
        )
        self.assertEqual(ok.at_user, "10001")
        self.assertIsNone(rejected.at_user)
        self.assertEqual(rejected.text, "你好 [[at:99999]]")

    def test_only_the_first_meme_tag_is_looked_up(self):
        catalog = _Catalog()
        asyncio.run(ReplyMediaProcessor(catalog).compose("看图 [[meme:happy]]"))
        self.assertEqual(catalog.calls, ["happy"])


class MediaUrlPolicyTests(unittest.TestCase):
    def test_static_url_checks(self):
        self.assertTrue(valid_media_url("https://cdn.example.com/a.png"))
        self.assertFalse(valid_media_url("http://user:pw@cdn.example.com/a.png"))
        self.assertFalse(valid_media_url("file:///etc/passwd"))
        self.assertFalse(valid_media_url("javascript:alert(1)"))
        self.assertFalse(valid_media_url("https://" + "a" * 9000 + "/x.png"))
        self.assertFalse(valid_media_url(""))

    def test_policy_reports_the_reason(self):
        policy = MediaUrlPolicy(max_bytes=1024)
        self.assertIsNone(policy.check("https://cdn.example.com/a.png"))
        self.assertEqual(policy.check(""), "empty url")
        self.assertIn("scheme", policy.check("ftp://cdn.example.com/a.png"))
        self.assertIn("mime", policy.check("https://cdn.example.com/a", content_type="text/html"))
        self.assertIn("too large", policy.check("https://cdn.example.com/a.png", size=2048))
        self.assertTrue(
            policy.accepts(
                "https://cdn.example.com/a.png", content_type="image/png; charset=binary", size=10
            )
        )

    def test_extension_allowlist_is_opt_in(self):
        policy = MediaUrlPolicy(require_extension=True)
        self.assertIn("extension", policy.check("https://cdn.example.com/a.exe") or "")
        self.assertIsNone(policy.check("https://cdn.example.com/a.PNG"))


class EnvConfigTests(unittest.TestCase):
    def test_limits_have_safe_defaults_and_clamp(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(max_image_bytes(), 4 * 1024 * 1024)
            self.assertEqual(max_url_chars(), 2048)
        with mock.patch.dict("os.environ", {"BOT_MEDIA_MAX_IMAGE_BYTES": "999999999"}):
            self.assertEqual(max_image_bytes(), 64 * 1024 * 1024)
        with mock.patch.dict("os.environ", {"BOT_MEDIA_MAX_URL_CHARS": "1"}):
            self.assertEqual(max_url_chars(), 64)
        with mock.patch.dict("os.environ", {"BOT_MEDIA_MAX_IMAGE_BYTES": "not-a-number"}):
            self.assertEqual(max_image_bytes(), 4 * 1024 * 1024)


class SendKwargsTests(unittest.TestCase):
    def test_send_kwargs_omit_empty_fields(self):
        self.assertEqual(
            OutboundMessage((Text("hi"),)).send_kwargs(), {"text": "hi"}
        )
        self.assertEqual(
            OutboundMessage((Text("hi"), At("7"))).send_kwargs(),
            {"text": "hi", "at_user": "7"},
        )
        self.assertEqual(OutboundMessage(()).send_kwargs(), {})


class GatewayAtTests(unittest.TestCase):
    def _send(self, **kwargs):
        gateway = OneBotGateway("127.0.0.1", 0, "secret")
        connection = _FakeConnection()
        captured = {}

        async def run():
            task = asyncio.create_task(gateway.handle(connection))
            await _settle(lambda: gateway.connection is connection)
            pending = asyncio.create_task(gateway.send(**kwargs))
            await _settle(lambda: connection.sent)
            captured.update(json.loads(connection.sent[0]))
            echo = captured["echo"]
            await connection.feed(
                json.dumps({"status": "ok", "retcode": 0, "echo": echo, "data": {}})
            )
            await pending
            await connection.close()
            await task

        asyncio.run(run())
        return captured["params"]["message"]

    def test_a_known_member_is_at_ed(self):
        message = self._send(
            group_id="42", text="hi", at_user="10001", allowed_at={"10001"}
        )
        self.assertEqual(message[0], {"type": "at", "data": {"qq": "10001"}})

    def test_an_out_of_roster_target_is_dropped_not_sent(self):
        message = self._send(
            group_id="42", text="hi", at_user="99999", allowed_at={"10001"}
        )
        self.assertEqual([p["type"] for p in message], ["text"])

    def test_no_roster_means_the_target_is_trusted_as_before(self):
        message = self._send(group_id="42", text="hi", at_user="99999")
        self.assertEqual(message[0], {"type": "at", "data": {"qq": "99999"}})

    def test_media_parts_are_sent_as_onebot_segments(self):
        message = self._send(
            group_id="42", text="看图", image="base64://Z2lm", voice="http://tts.test/a.mp3"
        )
        self.assertEqual(
            [p["type"] for p in message], ["text", "image", "record"]
        )


if __name__ == "__main__":
    unittest.main()
