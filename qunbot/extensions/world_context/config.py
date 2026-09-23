"""world_context reads its own environment, like the other optional packages.

The core ``Config`` stays free of world-perception fields. Everything here
defaults to *off*: enabling the extension and enabling each provider are two
separate decisions, so a deployment can import the package and still make no
network request and add no prompt contribution.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_WEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _number(name: str, default: float, *, low: float, high: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class WorldContextConfig:
    time_enabled: bool = False
    weather_enabled: bool = False
    replay_enabled: bool = False
    timezone: str = "Asia/Shanghai"
    weather_base_url: str = DEFAULT_WEATHER_BASE_URL
    weather_api_key: str = ""
    weather_city: str = ""
    weather_refresh_minutes: int = 30
    weather_timeout_seconds: float = 5.0
    replay_limit: int = 3

    @property
    def weather_active(self) -> bool:
        """Whether the weather provider can actually run.

        A fetch needs an endpoint, a key and a place to ask about. Missing any
        of the three silently disables the provider — the package still
        registers and starts, it just contributes nothing and creates no HTTP
        client. This is the "ships disabled without an API key" contract.
        """
        return (
            self.weather_enabled
            and bool(self.weather_base_url)
            and bool(self.weather_api_key)
            and bool(self.weather_city)
        )

    @classmethod
    def from_env(cls) -> WorldContextConfig:
        timezone = (
            os.getenv("BOT_WORLD_TIMEZONE", "").strip()
            or os.getenv("BOT_TIMEZONE", "").strip()
            or "Asia/Shanghai"
        )
        return cls(
            time_enabled=_flag("BOT_WORLD_TIME_ENABLED", False),
            weather_enabled=_flag("BOT_WORLD_WEATHER_ENABLED", False),
            replay_enabled=_flag("BOT_WORLD_REPLAY_ENABLED", False),
            timezone=timezone,
            weather_base_url=os.getenv(
                "BOT_WORLD_WEATHER_BASE_URL", DEFAULT_WEATHER_BASE_URL
            ).strip(),
            weather_api_key=os.getenv("BOT_WORLD_WEATHER_API_KEY", "").strip(),
            weather_city=os.getenv("BOT_WORLD_WEATHER_CITY", "").strip(),
            weather_refresh_minutes=int(
                _number("BOT_WORLD_WEATHER_REFRESH_MINUTES", 30, low=1, high=1440)
            ),
            weather_timeout_seconds=_number(
                "BOT_WORLD_WEATHER_TIMEOUT_SECONDS", 5.0, low=0.5, high=60.0
            ),
            replay_limit=int(_number("BOT_WORLD_REPLAY_LIMIT", 3, low=1, high=10)),
        )
