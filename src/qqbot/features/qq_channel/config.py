from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _items(name: str) -> frozenset[str]:
    return frozenset(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class QQChannelConfig:
    enabled: bool
    app_id: str
    app_secret: str
    sandbox: bool
    operator_ids: frozenset[str]
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "QQChannelConfig":
        enabled = _flag("BOT_QQ_CHANNEL_ENABLED")
        config = cls(
            enabled=enabled,
            app_id=os.getenv("BOT_QQ_CHANNEL_APP_ID", "").strip(),
            app_secret=os.getenv("BOT_QQ_CHANNEL_APP_SECRET", "").strip(),
            sandbox=_flag("BOT_QQ_CHANNEL_SANDBOX"),
            operator_ids=_items("BOT_QQ_CHANNEL_OPERATOR_IDS"),
            timeout_seconds=float(os.getenv("BOT_QQ_CHANNEL_TIMEOUT_SECONDS", "15")),
        )
        if enabled and (not config.app_id or not config.app_secret):
            raise ValueError("QQ channel requires BOT_QQ_CHANNEL_APP_ID and APP_SECRET")
        if not 1 <= config.timeout_seconds <= 60:
            raise ValueError("BOT_QQ_CHANNEL_TIMEOUT_SECONDS must be 1..60")
        return config
