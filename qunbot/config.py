"""The single environment parsing point.

Two rules keep this module honest:

1. **Core settings only.** A deployment knob for an optional feature (a poster
   date, a TTS voice, a memes directory) is parsed and validated inside the
   package that owns the feature. Adding such a field here would make an
   optional extension a startup dependency of the pure chat bot. The guard is
   ``test_config.py::CoreConfigScopeTests``.

2. **Cross-field checks, not just per-field parsing.** Each variable used to be
   clamped on its own, so a deployment could be individually well-formed and
   jointly nonsense — quiet hours that start after they end, a port of 70000, a
   timezone that does not exist. :meth:`Config.validate` runs in ``from_env``
   (structural rules only) and again at startup (deployment rules), so a bad
   configuration fails before anything connects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .domain import ConfigError, normalize_reasoning_effort

#: Version of the *configuration contract* this build understands: the set and
#: meaning of the BOT_* settings. Bump it when a release changes what an
#: existing setting means or removes one, so a deployment can tell whether its
#: .env was written for this build. ``--check`` prints it.
CONFIG_VERSION = 1


def ids(name: str) -> frozenset[str]:
    return frozenset(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    model_base_url: str
    model_api_key: str
    model_name: str
    onebot_host: str
    onebot_port: int
    onebot_token: str
    db_path: Path
    group_allowlist: frozenset[str]
    private_enabled: bool
    memory_extract_every: int
    affection_auto_enabled: bool
    timezone: str
    job_daily_limit: int
    active_start_hour: int
    active_end_hour: int
    job_cooldown_minutes: int
    job_freshness_minutes: int
    job_max_chars: int
    skills_path: Path
    persona_path: Path
    schedules_path: Path
    # Concurrency and backpressure. These bound the two queues in the system:
    # the inbound per-group lanes and the post-reply observation queue.
    observer_queue_size: int = 256
    observer_workers: int = 1
    observer_dedupe: int = 4096
    onebot_inbound_backlog: int = 64
    onebot_max_lanes: int = 32
    onebot_request_timeout: float = 20.0
    onebot_max_frame_kb: int = 1024
    # Short-term room scene. These three independent bounds keep the window
    # adaptive without allowing a busy group to explode prompt cost.
    conversation_window_messages: int = 40
    conversation_window_seconds: int = 300
    conversation_window_chars: int = 4800
    # Reasoning budget sent to the provider, or "" to send no field. A chat bot
    # spends most of its latency thinking: measured against the local proxy, a
    # short reply cost ~1500 reasoning tokens at the default and ~830 at "low",
    # which was the difference between a 30s and an 18s turn.
    model_reasoning_effort: str = ""
    extensions: frozenset[str] = frozenset({"scheduled_chat"})
    # Which contract version this configuration claims to satisfy. Stamped by
    # from_env; validated so a .env written for a newer build is refused rather
    # than half-understood.
    config_version: int = CONFIG_VERSION

    # --- validation ------------------------------------------------------

    def validate(
        self, *, require_secrets: bool = True, require_model_key: bool = True
    ) -> None:
        """Raise :class:`ConfigError` if this configuration cannot be run.

        ``require_secrets`` covers the token and the group allowlist — a bot
        with neither has nothing to connect to and nobody to talk to.
        ``require_model_key`` is skipped by ``--check``, which must work on a
        machine that has not been given the key yet.
        """
        self._check_consistency()
        if require_secrets:
            if not self.onebot_token:
                raise ConfigError("BOT_ONEBOT_TOKEN is required")
            if not self.group_allowlist:
                raise ConfigError("BOT_GROUP_ALLOWLIST is required")
        if require_model_key and not self.model_api_key:
            raise ConfigError("BOT_MODEL_API_KEY is required")

    def _check_consistency(self) -> None:
        """Rules that must hold for any deployment, including ``--check``."""
        if not 1 <= self.config_version <= CONFIG_VERSION:
            raise ConfigError(
                f"config version {self.config_version} is not supported by this "
                f"build (expected 1..{CONFIG_VERSION})"
            )
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ConfigError(
                f"BOT_TIMEZONE {self.timezone!r} is not a known IANA timezone"
            ) from error
        if not 0 <= self.active_start_hour <= 23:
            raise ConfigError(
                "BOT_ACTIVE_START_HOUR must be between 0 and 23, "
                f"got {self.active_start_hour}"
            )
        if not 1 <= self.active_end_hour <= 24:
            raise ConfigError(
                "BOT_ACTIVE_END_HOUR must be between 1 and 24, "
                f"got {self.active_end_hour}"
            )
        # An inverted window is not an empty window: every scheduled post and
        # every proactive turn would be silently skipped forever.
        if self.active_start_hour >= self.active_end_hour:
            raise ConfigError(
                "BOT_ACTIVE_START_HOUR must be earlier than BOT_ACTIVE_END_HOUR, "
                f"got {self.active_start_hour} and {self.active_end_hour}"
            )
        if not 1 <= self.onebot_port <= 65535:
            raise ConfigError(
                f"BOT_ONEBOT_PORT must be between 1 and 65535, got {self.onebot_port}"
            )
        if not self.model_name.strip():
            raise ConfigError("BOT_MODEL_NAME must not be empty")
        if not self.model_base_url.startswith(("http://", "https://")):
            raise ConfigError(
                "BOT_MODEL_BASE_URL must be an http(s) URL, "
                f"got {self.model_base_url!r}"
            )
        if self.onebot_request_timeout <= 0:
            raise ConfigError(
                "BOT_ONEBOT_REQUEST_TIMEOUT must be positive, "
                f"got {self.onebot_request_timeout}"
            )
        if not 4 <= self.conversation_window_messages <= 80:
            raise ConfigError("BOT_CONVERSATION_WINDOW_MESSAGES must be 4..80")
        if not 30 <= self.conversation_window_seconds <= 3600:
            raise ConfigError("BOT_CONVERSATION_WINDOW_SECONDS must be 30..3600")
        if not 400 <= self.conversation_window_chars <= 12000:
            raise ConfigError("BOT_CONVERSATION_WINDOW_CHARS must be 400..12000")
        # Checked here rather than at the first reply: a typo would otherwise
        # surface as slow turns with no explanation.
        normalize_reasoning_effort(self.model_reasoning_effort)

    @classmethod
    def from_env(cls) -> Config:
        def integer(key: str, default: int) -> int:
            return int(os.getenv(key, str(default)))

        config = cls(
            model_base_url=os.getenv(
                "BOT_MODEL_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            model_api_key=os.getenv("BOT_MODEL_API_KEY", ""),
            model_name=os.getenv("BOT_MODEL_NAME", "deepseek-chat"),
            model_reasoning_effort=os.getenv("BOT_MODEL_REASONING_EFFORT", ""),
            onebot_host=os.getenv("BOT_ONEBOT_HOST", "127.0.0.1"),
            onebot_port=integer("BOT_ONEBOT_PORT", 6199),
            onebot_token=os.getenv("BOT_ONEBOT_TOKEN", ""),
            db_path=Path(os.getenv("BOT_DB_PATH", "./data/qunbot.sqlite3")),
            group_allowlist=ids("BOT_GROUP_ALLOWLIST"),
            private_enabled=os.getenv("BOT_PRIVATE_ENABLED", "false").lower() == "true",
            memory_extract_every=max(2, integer("BOT_MEMORY_EXTRACT_EVERY", 8)),
            affection_auto_enabled=os.getenv(
                "BOT_AFFECTION_AUTO_ENABLED", "true"
            ).lower()
            == "true",
            timezone=os.getenv("BOT_TIMEZONE", "Asia/Shanghai"),
            job_daily_limit=max(0, integer("BOT_JOB_DAILY_LIMIT", 6)),
            active_start_hour=max(0, min(23, integer("BOT_ACTIVE_START_HOUR", 7))),
            active_end_hour=max(1, min(24, integer("BOT_ACTIVE_END_HOUR", 23))),
            job_cooldown_minutes=max(0, integer("BOT_JOB_COOLDOWN_MINUTES", 30)),
            job_freshness_minutes=max(0, integer("BOT_JOB_FRESHNESS_MINUTES", 180)),
            job_max_chars=max(20, integer("BOT_JOB_MAX_CHARS", 150)),
            skills_path=Path(os.getenv("BOT_SKILLS_PATH", "./skills")),
            persona_path=Path(os.getenv("BOT_PERSONA_PATH", "./config/persona.md")),
            schedules_path=Path(
                os.getenv("BOT_SCHEDULES_PATH", "./config/schedules.json")
            ),
            observer_queue_size=max(1, integer("BOT_OBSERVER_QUEUE_SIZE", 256)),
            observer_workers=max(1, integer("BOT_OBSERVER_WORKERS", 1)),
            observer_dedupe=max(1, integer("BOT_OBSERVER_DEDUPE", 4096)),
            onebot_inbound_backlog=max(1, integer("BOT_ONEBOT_INBOUND_BACKLOG", 64)),
            onebot_max_lanes=max(1, integer("BOT_ONEBOT_MAX_LANES", 32)),
            onebot_request_timeout=max(
                1.0, float(os.getenv("BOT_ONEBOT_REQUEST_TIMEOUT", "20.0"))
            ),
            onebot_max_frame_kb=max(1, integer("BOT_ONEBOT_MAX_FRAME_KB", 1024)),
            conversation_window_messages=integer(
                "BOT_CONVERSATION_WINDOW_MESSAGES", 40
            ),
            conversation_window_seconds=integer(
                "BOT_CONVERSATION_WINDOW_SECONDS", 300
            ),
            conversation_window_chars=integer(
                "BOT_CONVERSATION_WINDOW_CHARS", 4800
            ),
            extensions=(
                ids("BOT_EXTENSIONS")
                if "BOT_EXTENSIONS" in os.environ
                else frozenset({"scheduled_chat"})
            ),
            config_version=CONFIG_VERSION,
        )
        # Structural rules only: an empty environment must still parse, because
        # ``--check`` and the unit tests both build a Config without secrets.
        config.validate(require_secrets=False, require_model_key=False)
        return config
