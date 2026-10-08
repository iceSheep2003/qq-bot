"""The manual NapCat smoke command cannot send outside the configured groups."""

from pathlib import Path
import runpy
import unittest


class VoiceSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "voice_smoke.py"), run_name="voice_smoke_test")

    def test_group_allowlist_is_exact(self):
        allowed = self.module["group_is_allowed"]
        self.assertTrue(allowed(330707267, "866795853, 330707267"))
        self.assertFalse(allowed(33070726, "330707267"))
        self.assertFalse(allowed(330707267, ""))
