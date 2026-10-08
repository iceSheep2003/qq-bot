"""CosyVoice's different endpoint and constrained expressive SSML."""

import unittest

from qunbot.extensions.voice.sources import CosyVoiceSpeechSource, QwenAudioSpeechSource


class CosyVoiceSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = CosyVoiceSpeechSource(
            "https://dashscope.aliyuncs.com/api/v1", "test-key",
            "cosyvoice-v3-flash", "longfeifei_v3",
        )

    def tearDown(self):
        import asyncio
        asyncio.run(self.source.close())

    def test_endpoint_and_voice(self):
        self.assertTrue(self.source.endpoint.endswith("/services/audio/tts/SpeechSynthesizer"))
        payload = self.source.request_body("可以呀")
        self.assertEqual(payload["model"], "cosyvoice-v3-flash")
        self.assertEqual(payload["input"]["voice"], "longfeifei_v3")
        self.assertTrue(payload["input"]["enable_ssml"])

    def test_light_expression_and_xml_escaping(self):
        quiet = self.source.expressive_text("别急，慢慢来 <3")
        lively = self.source.expressive_text("哈哈，可以呀！")
        self.assertIn('rate="0.97"', quiet)
        self.assertIn("&lt;3", quiet)
        self.assertIn('rate="1.04"', lively)
        self.assertTrue(lively.endswith("</speak>"))


class QwenAudioSourceTests(unittest.TestCase):
    def setUp(self):
        self.source = QwenAudioSpeechSource(
            "https://dashscope.aliyuncs.com/api/v1", "test-key",
            "qwen-audio-3.0-tts-flash", "longanfengyue",
        )

    def tearDown(self):
        import asyncio
        asyncio.run(self.source.close())

    def test_instruction_and_voice(self):
        body = self.source.request_body("别急，先歇一会儿。", "warm")
        self.assertEqual(body["input"]["voice"], "longanfengyue")
        self.assertEqual(body["input"]["text"], "别急，先歇一会儿。")
        self.assertIn("轻声安慰", body["input"]["instruction"])
        self.assertNotIn("enable_ssml", body["input"])
