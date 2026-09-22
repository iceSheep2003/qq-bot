"""Framework-independent domain values."""

from __future__ import annotations

from dataclasses import dataclass


class JobSkipped(Exception):
    """A scheduled run declined to post. Expected, not an error.

    Raised for quiet hours, exhausted quota or a group that went quiet, so the
    scheduler records ``skipped`` instead of ``failed`` and stops logging noise.
    """


@dataclass(frozen=True)
class MessageEvent:
    event_id: str
    scope: str
    group_id: str | None
    user_id: str
    nickname: str
    text: str
    image_urls: tuple[str, ...]
    at_bot: bool
    at_users: tuple[str, ...]
    timestamp: int
