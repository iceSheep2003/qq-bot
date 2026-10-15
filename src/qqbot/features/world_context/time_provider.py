"""Time as a dynamic-context contribution.

The wall clock belongs in the *dynamic suffix*, never the stable prefix: every
turn has a different minute, so a timestamp anywhere in the cached prefix would
invalidate the provider's cache on each request. This module only ever returns
a string to ``ContextRegistry``; it cannot reach the persona.

The value is produced entirely from the deployer's own host clock and a fixed
format string — no third-party bytes enter it — which is why it is registered
at ``Trust.DEPLOYER`` rather than as untrusted data.
"""

from __future__ import annotations

import time
from datetime import datetime, tzinfo

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _load_zone(name: str) -> tzinfo | None:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        # A missing tzdata must not stop the bot from speaking. Falling back to
        # local time keeps the line approximately right instead of failing the
        # whole turn.
        return None


def _part_of_day(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


class TimeProvider:
    """Renders "now" for the current turn. Cheap, synchronous, infallible."""

    def __init__(self, timezone: str = "Asia/Shanghai", *, now=None):
        self._zone = _load_zone(timezone)
        self._zone_name = timezone if self._zone is not None else "本地时间"
        self._now = now or time.time

    def __call__(self, _event) -> str:
        moment = datetime.fromtimestamp(self._now(), tz=self._zone)
        return (
            f"当前时间：{moment:%Y-%m-%d %H:%M} "
            f"{_WEEKDAYS[moment.weekday()]} {_part_of_day(moment.hour)}"
            f"（{self._zone_name}）"
        )
