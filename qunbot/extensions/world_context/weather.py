"""Weather fetched from an external endpoint and downgraded to plain data.

Three constraints shape this module.

*Ships disabled without a key.* ``WorldContextConfig.weather_active`` is false
unless an endpoint, a key and a city are all present, and ``register()`` only
constructs a :class:`WeatherService` when it is true. No key means no
``httpx`` client, therefore no request, while the bot starts normally.

*Fetch outside the turn.* ``ContextRegistry`` providers are synchronous, and a
weather API must not block the event loop on every reply. The service refreshes
on a background loop into an in-memory snapshot; the provider is a pure read of
that snapshot and returns ``None`` until the first successful fetch. A failed
fetch clears the snapshot rather than serving a stale lie, and the failure is
caught here so it can never escape into the reply path.

*Treat the response as hostile.* The endpoint is a third party: its body can
contain arbitrary text, including text shaped like instructions. It is
registered at ``Trust.HOSTILE`` and every field is flattened to a single line
and length-capped before it reaches the prompt.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

#: Anything that could break out of the single prompt line we build.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE = re.compile(r"\s+")
#: Hard ceiling for the rendered snapshot; matches the registry max_chars.
SNAPSHOT_MAX_CHARS = 200


def _sanitize(value: object, limit: int = 60) -> str:
    """Flatten arbitrary third-party text to one bounded line."""
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    flat = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    return flat[:limit]


def reading_from_payload(payload: object, city: str, *, now: float | None = None) -> str | None:
    """Build the one-line snapshot from a provider JSON body, tolerantly.

    A shape we do not recognise yields ``None`` rather than a guess: an
    unparseable forecast is worth less than no forecast.
    """
    if not isinstance(payload, dict):
        return None
    description = ""
    weather = payload.get("weather")
    if isinstance(weather, list) and weather and isinstance(weather[0], dict):
        description = _sanitize(weather[0].get("description"), 40)
    temperature = ""
    main = payload.get("main")
    if isinstance(main, dict):
        raw_temp = main.get("temp")
        if isinstance(raw_temp, (int, float)) and not isinstance(raw_temp, bool):
            temperature = f"{round(float(raw_temp))}℃"
    if not description and not temperature:
        return None
    place = _sanitize(payload.get("name") or city, 24)
    parts = [p for p in (description, temperature) if p]
    moment = time.strftime("%H:%M", time.localtime(now if now is not None else time.time()))
    return _sanitize(f"当前天气（{place}）：{'，'.join(parts)}（{moment} 更新）", SNAPSHOT_MAX_CHARS)


class WeatherService:
    """Owns the HTTP client, the refresh loop and the cached snapshot."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        city: str,
        timeout: float = 5.0,
        interval_minutes: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
        now=None,
    ):
        self._base_url = base_url
        self._api_key = api_key
        self._city = city
        self._interval = max(1, int(interval_minutes)) * 60
        self._now = now
        # One client per enabled service. Constructed lazily in the sense that
        # nothing builds a WeatherService at all when weather is off or unkeyed.
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._snapshot: str | None = None

    @property
    def snapshot(self) -> str | None:
        return self._snapshot

    async def refresh(self) -> str | None:
        """Fetch once. Never raises; a failure clears the snapshot."""
        try:
            response = await self._client.get(
                self._base_url,
                params={
                    "q": self._city,
                    "appid": self._api_key,
                    "units": "metric",
                    "lang": "zh_cn",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            # The reply must not fail because a forecast did not arrive. A
            # missed forecast is routine, so this is a one-line warning rather
            # than a traceback on every cycle.
            log.warning(
                "weather fetch failed (%s: %s); dropping world_weather this cycle",
                type(exc).__name__,
                exc,
            )
            self._snapshot = None
            return None
        self._snapshot = reading_from_payload(payload, self._city, now=self._now)
        return self._snapshot

    async def run(self) -> None:
        """Background loop started by ``FeatureHost.workers``."""
        while True:
            await self.refresh()
            await asyncio.sleep(self._interval)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # pragma: no cover - shutdown best effort
            log.debug("weather client close failed", exc_info=True)


class WeatherProvider:
    """Synchronous context contributor: reads the cached snapshot only."""

    def __init__(self, service: WeatherService):
        self._service = service

    def __call__(self, _event) -> str | None:
        return self._service.snapshot
