"""World perception — time, weather and memory replay — as three providers.

Reference: the "Connectome" direction in ``docs/DEVELOPMENT_ROADMAP.md``. The
roadmap also records its failure mode: a plugin that injects energy, time and
weather into the *system prompt* every turn can defeat prompt caching even when
LivingMemory is careful, because the "stable" prefix keeps changing.

This package avoids that by construction. It never touches the stable prefix.
Its only output path is ``host.context.register``, whose contributions
``Agent.build_messages`` renders into the *dynamic suffix* (the "可选扩展上下文"
block of the user message). The prefix — persona plus enabled-Skill catalogue —
is built without ever consulting this package, so enabling, disabling or
reconfiguring any provider cannot change a single byte of it.

Each provider is registered separately and can be switched off on its own:

``world_time``    deployer's own clock; ``Trust.DEPLOYER``, priority 10
``world_weather`` third-party HTTP; ``Trust.HOSTILE``, priority 80
``memory_replay`` read-only view of the memory port; ``Trust.DERIVED``, priority 75

Import discipline: the weather module (and therefore ``httpx.AsyncClient``) is
imported only when weather is enabled *and* fully configured, and the replay
module only when replay is enabled and a coordinator exists. A deployment that
runs the package with everything off imports none of them and creates no
database and no HTTP client.
"""

from __future__ import annotations

import logging

from ...runtime.context import Trust
from .config import WorldContextConfig

log = logging.getLogger(__name__)

TIME_NAME = "world_time"
WEATHER_NAME = "world_weather"
REPLAY_NAME = "memory_replay"

#: Budgets per provider. Weather is deliberately smallest and dropped first.
TIME_MAX_CHARS = 120
WEATHER_MAX_CHARS = 200
REPLAY_MAX_CHARS = 400


def _resolve_coordinator(memory, host):
    """The memory port is read-only here; whoever owns it decides the source.

    ``loader.build_features`` currently calls ``register(host, config, model)``
    with no memory argument, so the coordinator can come in two ways: an
    explicit fourth argument (a wiring change in ``app.py``) or an attribute the
    composition root sets on the host before features are built. Absent both,
    replay stays off rather than inventing a second memory store.
    """
    if memory is not None:
        return memory
    return getattr(host, "memory_coordinator", None)


def register(host, _config=None, _model=None, memory=None) -> None:
    config = WorldContextConfig.from_env()

    if config.time_enabled:
        from .time_provider import TimeProvider

        host.context.register(
            TIME_NAME,
            TimeProvider(config.timezone),
            trust=Trust.DEPLOYER,
            priority=10,
            max_chars=TIME_MAX_CHARS,
        )

    if config.weather_active:
        from .weather import WeatherProvider, WeatherService

        service = WeatherService(
            base_url=config.weather_base_url,
            api_key=config.weather_api_key,
            city=config.weather_city,
            timeout=config.weather_timeout_seconds,
            interval_minutes=config.weather_refresh_minutes,
        )
        host.context.register(
            WEATHER_NAME,
            WeatherProvider(service),
            trust=Trust.HOSTILE,
            priority=80,
            max_chars=WEATHER_MAX_CHARS,
        )
        # Refresh off the turn path, and close the client on shutdown.
        host.workers.append(service.run)
        host.closers.append(service.close)
    elif config.weather_enabled:
        log.info(
            "world_context weather enabled but unconfigured "
            "(need BOT_WORLD_WEATHER_API_KEY and BOT_WORLD_WEATHER_CITY); "
            "no client created, no request made"
        )

    if config.replay_enabled:
        coordinator = _resolve_coordinator(memory, host)
        if coordinator is None:
            log.warning(
                "world_context replay enabled but no MemoryCoordinator wired; "
                "memory replay stays off"
            )
        else:
            from .replay import MemoryReplayProvider

            host.context.register(
                REPLAY_NAME,
                MemoryReplayProvider(coordinator, config.replay_limit),
                trust=Trust.DERIVED,
                priority=75,
                max_chars=REPLAY_MAX_CHARS,
            )


def validate() -> dict:
    config = WorldContextConfig.from_env()
    return {
        "time": config.time_enabled,
        "weather_enabled": config.weather_enabled,
        "weather_active": config.weather_active,
        "replay": config.replay_enabled,
        "timezone": config.timezone,
    }
