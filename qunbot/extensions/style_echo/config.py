"""Style echo reads its own environment; the core Config stays free of it.

Two defaults matter more than the rest:

* ``enabled`` is **false**. Nothing runs, nothing is collected, unless the
  deployer switched the extension on in ``BOT_EXTENSIONS`` *and* set
  ``BOT_STYLE_ECHO_ENABLED=true``.
* ``allowed_users`` is **empty**. Even when the extension is enabled, an empty
  allow-list means no user is ever sampled. Consent is a deployer-side
  allow-list of user IDs, never a message a group member can send.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _ids(name: str) -> frozenset[str]:
    return frozenset(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


def _int(name: str, default: int, *, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class StyleEchoConfig:
    enabled: bool = False
    allowed_users: frozenset[str] = field(default_factory=frozenset)
    max_samples: int = 40
    min_samples: int = 5
    poll_seconds: int = 120
    retention_days: int = 30
    scan_limit: int = 40

    @classmethod
    def from_env(cls) -> StyleEchoConfig:
        return cls(
            enabled=_flag("BOT_STYLE_ECHO_ENABLED", False),
            allowed_users=_ids("BOT_STYLE_ECHO_ALLOWED_USERS"),
            max_samples=_int("BOT_STYLE_ECHO_MAX_SAMPLES", 40, low=5, high=500),
            min_samples=_int("BOT_STYLE_ECHO_MIN_SAMPLES", 5, low=2, high=100),
            poll_seconds=_int("BOT_STYLE_ECHO_POLL_SECONDS", 120, low=15, high=3600),
            retention_days=_int("BOT_STYLE_ECHO_RETENTION_DAYS", 30, low=1, high=3650),
            scan_limit=40,
        )

    @property
    def collecting(self) -> bool:
        """True only when switched on *and* at least one person consented."""
        return self.enabled and bool(self.allowed_users)

    def accepts(self, user_id: str) -> bool:
        """The single consent gate. No allow-list entry, no sample — ever.

        Requires both switches: the extension being enabled *and* this user
        being named by the deployer. Either one alone is not consent.
        """
        return self.enabled and bool(user_id) and user_id in self.allowed_users
