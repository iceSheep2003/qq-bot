"""Opt-in style echo: learn an allow-listed speaker's *writing habits* only.

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

Registering contributes one context provider, one post-reply observer and one
background loop — all of which do nothing at all until the deployer both enables
the extension and lists at least one user.
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
    """Wire the feature. Disabled or unconsented deployments import nothing heavy.

    Mirrors ``mood.register``: the environment is read here, and a disabled or
    empty-roster configuration returns before any database is opened.
    """
    echo_config = StyleEchoConfig.from_env()
    # Two independent off-switches: the extension allow-list (loader decides
    # whether this function runs at all) and this env flag. And even when both
    # are on, an empty allow-list collects nobody, so there is nothing to wire.
    if not echo_config.collecting:
        return

    from ...runtime.context import Trust
    from ...storage.conversation import ConversationStore
    from ...storage.database import SqliteDatabase
    from ...storage.style import StyleStore

    # The style table lives in the bot's own SQLite file; storage/database.py
    # discovers ``storage/style.py`` and creates it. One extra connection keeps
    # the feature from reaching into runtime/service.py or its private fields.
    database = SqliteDatabase(config.db_path)
    store = StyleStore(database)
    conversations = ConversationStore(database)
    echo = StyleEcho(
        echo_config,
        store,
        conversations.recent,
        frozenset(getattr(config, "group_allowlist", frozenset())),
    )

    # Style, not identity: derived, below the speaker's own history, and capped
    # so it can never crowd out what the conversation actually needs.
    host.context.register(
        "style_echo",
        echo.guidance,
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
        "allowed_users": len(echo_config.allowed_users),
        "collecting": echo_config.collecting,
        "max_samples": echo_config.max_samples,
        "retention_days": echo_config.retention_days,
    }
