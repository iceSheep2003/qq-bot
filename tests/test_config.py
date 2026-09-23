"""Config is the single environment parsing point, and it is core-only.

Two things are pinned here that used to be conventions rather than guarantees:

* the settings this module reads are exactly the *core* ones — an optional
  feature's knob is parsed inside the package that owns it, so a bot that
  disables that feature never depends on its settings existing;
* a deployment that is individually well-formed but jointly impossible (quiet
  hours that start after they end, a port of 70000) fails at parse time instead
  of at 7am.
"""

from __future__ import annotations

import os
import re
import unittest
from dataclasses import fields
from pathlib import Path
from unittest import mock

from qunbot.config import CONFIG_VERSION, Config
from qunbot.domain import ConfigError

ROOT = Path(__file__).resolve().parent.parent

#: Every BOT_* name config.py is allowed to read. Adding an optional feature's
#: setting here is the thing this list exists to prevent.
CORE_SETTINGS = {
    "BOT_ACTIVE_END_HOUR",
    "BOT_ACTIVE_START_HOUR",
    "BOT_AFFECTION_AUTO_ENABLED",
    "BOT_DB_PATH",
    "BOT_EXTENSIONS",
    "BOT_GROUP_ALLOWLIST",
    "BOT_JOB_COOLDOWN_MINUTES",
    "BOT_JOB_DAILY_LIMIT",
    "BOT_JOB_FRESHNESS_MINUTES",
    "BOT_JOB_MAX_CHARS",
    "BOT_MEMORY_EXTRACT_EVERY",
    "BOT_MODEL_API_KEY",
    "BOT_MODEL_BASE_URL",
    "BOT_MODEL_NAME",
    "BOT_MODEL_REASONING_EFFORT",
    "BOT_OBSERVER_DEDUPE",
    "BOT_OBSERVER_QUEUE_SIZE",
    "BOT_OBSERVER_WORKERS",
    "BOT_ONEBOT_HOST",
    "BOT_ONEBOT_INBOUND_BACKLOG",
    "BOT_ONEBOT_MAX_FRAME_KB",
    "BOT_ONEBOT_MAX_LANES",
    "BOT_ONEBOT_PORT",
    "BOT_ONEBOT_REQUEST_TIMEOUT",
    "BOT_ONEBOT_TOKEN",
    "BOT_PERSONA_PATH",
    "BOT_PRIVATE_ENABLED",
    "BOT_SCHEDULES_PATH",
    "BOT_SKILLS_PATH",
    "BOT_TIMEZONE",
}

#: Settings that belong to an optional feature and must stay in its package.
OPTIONAL_SETTINGS = (
    "BOT_TTS_PROVIDER",
    "BOT_TTS_BASE_URL",
    "BOT_TTS_API_KEY",
    "BOT_TTS_MODEL",
    "BOT_TTS_VOICE",
    "BOT_MEMES_PATH",
    "BOT_EXAM_DATE",
    "BOT_POSTER_FONT",
    "BOT_MOOD_DB_PATH",
    "BOT_MOOD_SENSITIVITY",
    "BOT_SLANG_ENABLED",
    "BOT_STYLE_ECHO_ENABLED",
    "BOT_REPLY_POLICY_MODE",
    "BOT_WORLD_WEATHER_CITY",
    "BOT_PROACTIVE_DAILY_LIMIT",
)


def env(**values: str):
    """A cleared environment with exactly ``values`` in it."""
    return mock.patch.dict(os.environ, values, clear=True)


class CoreConfigScopeTests(unittest.TestCase):
    """Config carries core settings only. Optional ones live in their package."""

    def test_config_module_reads_only_core_settings(self):
        source = (ROOT / "qunbot" / "config.py").read_text(encoding="utf-8")
        read = set(re.findall(r'"(BOT_[A-Z0-9_]+)"', source))
        self.assertEqual(read, CORE_SETTINGS)

    def test_no_optional_feature_setting_appears_in_config(self):
        source = (ROOT / "qunbot" / "config.py").read_text(encoding="utf-8")
        leaked = [name for name in OPTIONAL_SETTINGS if name in source]
        self.assertEqual(leaked, [], f"optional settings leaked into Config: {leaked}")

    def test_config_has_no_optional_feature_fields(self):
        names = {field.name for field in fields(Config)}
        forbidden = {
            "exam_date",
            "poster_font",
            "tts_provider",
            "tts_voice",
            "memes_path",
            "weather_city",
            "mood_db_path",
        }
        self.assertEqual(names & forbidden, set())

    def test_optional_settings_are_validated_inside_their_own_package(self):
        """The voice extension still owns its own four-field requirement."""
        from qunbot.extensions.voice import register as voice

        with env():
            with self.assertRaises(ValueError) as caught:
                voice._settings()
        self.assertIn("BOT_TTS_", str(caught.exception))


class ConfigVersionTests(unittest.TestCase):
    def test_from_env_stamps_the_contract_version(self):
        with env():
            config = Config.from_env()
        self.assertEqual(config.config_version, CONFIG_VERSION)

    def test_a_config_from_a_newer_build_is_refused(self):
        with env():
            config = Config.from_env()
        stale = Config(**{**config.__dict__, "config_version": CONFIG_VERSION + 1})
        with self.assertRaises(ConfigError) as caught:
            stale.validate(require_secrets=False, require_model_key=False)
        self.assertIn("config version", str(caught.exception))

    def test_zero_is_not_a_valid_version(self):
        with env():
            config = Config.from_env()
        broken = Config(**{**config.__dict__, "config_version": 0})
        with self.assertRaises(ConfigError):
            broken.validate(require_secrets=False, require_model_key=False)


class CrossFieldValidationTests(unittest.TestCase):
    """Values that are each fine but jointly impossible must fail at parse."""

    def test_defaults_are_valid(self):
        with env():
            Config.from_env()

    def test_active_window_is_inclusive_at_both_ends(self):
        with env(BOT_ACTIVE_START_HOUR="0", BOT_ACTIVE_END_HOUR="24"):
            self.assertEqual(Config.from_env().active_end_hour, 24)

    def test_start_after_end_is_rejected(self):
        with env(BOT_ACTIVE_START_HOUR="22", BOT_ACTIVE_END_HOUR="7"):
            with self.assertRaises(ConfigError) as caught:
                Config.from_env()
        self.assertIn("earlier than", str(caught.exception))

    def test_equal_hours_are_rejected(self):
        """An empty window every day is a bug, not a quiet bot."""
        with env(BOT_ACTIVE_START_HOUR="9", BOT_ACTIVE_END_HOUR="9"):
            with self.assertRaises(ConfigError):
                Config.from_env()

    def test_unknown_timezone_is_rejected(self):
        with env(BOT_TIMEZONE="Mars/Olympus_Mons"):
            with self.assertRaises(ConfigError) as caught:
                Config.from_env()
        self.assertIn("BOT_TIMEZONE", str(caught.exception))

    def test_out_of_range_port_is_rejected(self):
        with env(BOT_ONEBOT_PORT="70000"):
            with self.assertRaises(ConfigError) as caught:
                Config.from_env()
        self.assertIn("BOT_ONEBOT_PORT", str(caught.exception))

    def test_non_http_model_url_is_rejected(self):
        with env(BOT_MODEL_BASE_URL="ftp://model.example"):
            with self.assertRaises(ConfigError) as caught:
                Config.from_env()
        self.assertIn("BOT_MODEL_BASE_URL", str(caught.exception))

    def test_empty_model_name_is_rejected(self):
        with env(BOT_MODEL_NAME="   "):
            with self.assertRaises(ConfigError) as caught:
                Config.from_env()
        self.assertIn("BOT_MODEL_NAME", str(caught.exception))

    def test_an_empty_environment_still_parses(self):
        """--check and the unit tests build a Config before secrets exist."""
        with env():
            config = Config.from_env()
        self.assertEqual(config.onebot_token, "")
        self.assertEqual(config.group_allowlist, frozenset())


class DeploymentValidationTests(unittest.TestCase):
    def config(self, **values: str) -> Config:
        with env(**values):
            return Config.from_env()

    def test_token_is_required(self):
        with self.assertRaises(ConfigError) as caught:
            self.config(BOT_GROUP_ALLOWLIST="42", BOT_MODEL_API_KEY="k").validate()
        self.assertEqual(str(caught.exception), "BOT_ONEBOT_TOKEN is required")

    def test_allowlist_is_required(self):
        with self.assertRaises(ConfigError) as caught:
            self.config(BOT_ONEBOT_TOKEN="x", BOT_MODEL_API_KEY="k").validate()
        self.assertEqual(str(caught.exception), "BOT_GROUP_ALLOWLIST is required")

    def test_model_key_is_required_for_a_real_run(self):
        with self.assertRaises(ConfigError) as caught:
            self.config(BOT_ONEBOT_TOKEN="x", BOT_GROUP_ALLOWLIST="42").validate()
        self.assertEqual(str(caught.exception), "BOT_MODEL_API_KEY is required")

    def test_model_key_is_not_required_for_check(self):
        self.config(BOT_ONEBOT_TOKEN="x", BOT_GROUP_ALLOWLIST="42").validate(
            require_model_key=False
        )

    def test_a_complete_deployment_validates(self):
        self.config(
            BOT_ONEBOT_TOKEN="x", BOT_GROUP_ALLOWLIST="42", BOT_MODEL_API_KEY="k"
        ).validate()


if __name__ == "__main__":
    unittest.main()
