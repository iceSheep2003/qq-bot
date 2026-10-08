"""Learn group rhythm, or opt into individual writing-habit sampling.

This is not an impersonation feature and the code is arranged so it cannot
become one:

* Collection is gated on a deployer-side allow-list of user IDs read from the
  environment. No group message can add someone to it — there is no chat
  command, no tool and no job action here.
* What is learned is an aggregate description of sentence shape ("prefers short
  sentences; ends with ellipses"), rendered from a closed vocabulary. It is
  contributed to the **dynamic suffix** with ``Trust.DERIVED`` and a 250-char
  cap, and never written to the stable persona file or any stable-prefix source.
* The guidance text names nobody and uses no impersonation wording; a
  fail-closed guard (``guidance.assert_safe``) raises if one ever appears.
* Everything collected is deletable through ``StyleEcho.forget(user_id)``.

With no individual roster, the enabled extension derives only a temporary,
group-level statistical note from the existing conversation ledger. A roster
switches to the original, individually consented mode.
"""

from .config import StyleEchoConfig
from .guidance import StyleProfile, UnsafeGuidance, analyze, assert_safe, render
from .runner import StyleEcho

__all__ = [
    "StyleEcho",
    "StyleEchoConfig",
    "StyleProfile",
    "UnsafeGuidance",
    "analyze",
    "assert_safe",
    "build_worker",
    "register",
    "render",
    "validate",
]


def register(host, config, _model) -> None:
    """Wire exactly one group or individual style provider."""
    echo_config = StyleEchoConfig.from_env()
    if not echo_config.enabled:
        return

    from ...runtime.context import Trust, cached
    from ...storage.conversation import ConversationStore
    from ...storage.database import SqliteDatabase

    database = SqliteDatabase(config.db_path)
    conversations = ConversationStore(database)
    groups = frozenset(getattr(config, "group_allowlist", frozenset()))
    if echo_config.collecting:
        from ...storage.style import StyleStore

        echo = StyleEcho(echo_config, StyleStore(database), conversations.recent, groups)
        provider = cached(
            echo.guidance,
            ttl_seconds=echo_config.cache_seconds,
            key=lambda event: f"{event.scope}|{event.user_id}",
        )
    else:
        from .group import GroupStyle

        echo = GroupStyle(echo_config, conversations.recent, groups)
        for group_id in sorted(groups):
            echo.refresh(f"group:{group_id}")
        provider = echo.guidance

    # Style, not identity: derived, below the speaker's own history, and capped
    # so it can never crowd out what the conversation actually needs.
    host.context.register(
        "style_echo",
        provider,
        trust=Trust.DERIVED,
        priority=65,
        max_chars=250,
    )
    host.observers.append(echo)
    host.workers.append(echo.run)
    host.closers.append(database.close)


def validate() -> dict:
    """Startup report. Reads the environment only; opens no database."""
    echo_config = StyleEchoConfig.from_env()
    return {
        "enabled": echo_config.enabled,
        "mode": "individual" if echo_config.collecting else "group" if echo_config.enabled else "off",
        "allowed_users": len(echo_config.allowed_users),
        "collecting": echo_config.collecting,
        "max_samples": echo_config.max_samples,
        "retention_days": echo_config.retention_days,
    }
