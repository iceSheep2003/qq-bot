from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    extensions: frozenset[str] = frozenset({"scheduled_chat"})

    @classmethod
    def from_env(cls) -> Config:
        def integer(key: str, default: int) -> int:
            return int(os.getenv(key, str(default)))

        return cls(
            model_base_url=os.getenv(
                "BOT_MODEL_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            model_api_key=os.getenv("BOT_MODEL_API_KEY", ""),
            model_name=os.getenv("BOT_MODEL_NAME", "deepseek-chat"),
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
            extensions=(
                ids("BOT_EXTENSIONS")
                if "BOT_EXTENSIONS" in os.environ
                else frozenset({"scheduled_chat"})
            ),
        )
