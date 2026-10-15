from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _items(name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in os.getenv(name, "").split(",") if part.strip())


@dataclass(frozen=True)
class QQAdminConfig:
    operator_ids: frozenset[str]
    max_mute_seconds: int = 600
    join_review_enabled: bool = False
    join_approve_keywords: tuple[str, ...] = ()
    join_reject_keywords: tuple[str, ...] = ()
    join_default_approve: bool = False
    welcome_enabled: bool = True
    title_enabled: bool = True
    title_vote_threshold: int = 3
    title_proposal_seconds: int = 600
    title_max_chars: int = 6

    @classmethod
    def from_env(cls) -> "QQAdminConfig":
        maximum = int(os.getenv("BOT_QQ_ADMIN_MAX_MUTE_SECONDS", "600"))
        if not 0 <= maximum <= 86400:
            raise ValueError("BOT_QQ_ADMIN_MAX_MUTE_SECONDS must be 0..86400")
        votes = int(os.getenv("BOT_QQ_ADMIN_TITLE_VOTE_THRESHOLD", "3"))
        proposal_seconds = int(os.getenv("BOT_QQ_ADMIN_TITLE_PROPOSAL_SECONDS", "600"))
        title_max_chars = int(os.getenv("BOT_QQ_ADMIN_TITLE_MAX_CHARS", "6"))
        if not 2 <= votes <= 20:
            raise ValueError("BOT_QQ_ADMIN_TITLE_VOTE_THRESHOLD must be 2..20")
        if not 60 <= proposal_seconds <= 86400:
            raise ValueError("BOT_QQ_ADMIN_TITLE_PROPOSAL_SECONDS must be 60..86400")
        if not 1 <= title_max_chars <= 18:
            raise ValueError("BOT_QQ_ADMIN_TITLE_MAX_CHARS must be 1..18")
        return cls(
            operator_ids=frozenset(_items("BOT_QQ_ADMIN_OPERATOR_IDS")),
            max_mute_seconds=maximum,
            join_review_enabled=_flag("BOT_QQ_ADMIN_JOIN_REVIEW_ENABLED", False),
            join_approve_keywords=_items("BOT_QQ_ADMIN_JOIN_APPROVE_KEYWORDS"),
            join_reject_keywords=_items("BOT_QQ_ADMIN_JOIN_REJECT_KEYWORDS"),
            join_default_approve=_flag("BOT_QQ_ADMIN_JOIN_DEFAULT_APPROVE", False),
            welcome_enabled=_flag("BOT_QQ_ADMIN_WELCOME_ENABLED", True),
            title_enabled=_flag("BOT_QQ_ADMIN_TITLE_ENABLED", True),
            title_vote_threshold=votes,
            title_proposal_seconds=proposal_seconds,
            title_max_chars=title_max_chars,
        )
