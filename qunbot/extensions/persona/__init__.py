"""Dynamic Persona and bounded style-example evolution.

The dynamic per-turn hint remains in the suffix. Optional scheduled learning
may rotate only the marked example section of ``config/persona.md``. The
identity and rules stay fixed; the prefix changes at most on a gated scan,
then becomes cacheable again. Group messages cannot issue persona commands.

The split with ``qunbot/emotion/`` (the mood feature) is the other half of the
design:

* ``emotion/`` owns the bot's *state* — five dimensions that decay, stored in
  their own database, kept up to date by post-reply assessment. It is the only
  owner of that state.
* this package owns *no state at all*. It reads the mood (borrowed, read-only,
  if the mood feature happens to be installed) plus the situation of the turn,
  and turns them into one sentence about phrasing. Losing this package's whole
  cache changes nothing about how the bot feels; it only means the bot speaks
  in its baseline style again.

The intended priority is that when the dynamic budget runs short the model
keeps the causal state (mood) and loses the stylistic refinement, because the
refinement is re-derivable and the state is not. See the note on
``CONTEXT_PRIORITY`` below for how the number achieves that. A disabled
package imports nothing, and a disabled mood simply means the situation alone
is used.

Enabled only by an explicit ``BOT_PERSONA_ENABLED=true`` plus adding
``persona`` to the deployer's extension allowlist.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...runtime.context import Trust
from .config import PersonaConfig
from .director import PersonaDirector
from .strategy import (
    DEFAULT,
    LENGTHS,
    MAX_RENDERED_CHARS,
    TONES,
    StyleStrategy,
    parse_strategy,
)

log = logging.getLogger(__name__)

CONTEXT_NAME = "persona"
# Read ``ContextRegistry.collect`` before changing it: selection runs lowest
# number first and stops when the budget is spent, so a *smaller* number is
# kept *longer*. This must therefore sit **above** mood's 60 to get the
# intended ordering — under a tight budget the mood line (the cause) survives
# and this refinement (re-derivable) is dropped. It is also above the slang
# terms (70) and the replayed memory (75), and below the meme tag list (20),
# the clock (10) and the speaker's own history.
CONTEXT_PRIORITY = 65

__all__ = [
    "CONTEXT_NAME",
    "CONTEXT_PRIORITY",
    "DEFAULT",
    "LENGTHS",
    "MAX_RENDERED_CHARS",
    "TONES",
    "PersonaConfig",
    "PersonaDirector",
    "StyleStrategy",
    "parse_strategy",
    "register",
    "validate",
]


def register(host, app_config, model) -> None:
    config = PersonaConfig.from_env()
    if not config.enabled:
        return
    # ``host.proactive_gate`` is whatever the mood feature registered, and the
    # loader registers features in name order so ``mood`` is already there.
    # Duck-typed on purpose: this package never imports ``qunbot.emotion``, so
    # it cannot grow a second copy of that state, and a host without the
    # attribute is a supported configuration rather than a broken one.
    state_view = getattr(getattr(host, "proactive_gate", None), "narration", None)
    director = PersonaDirector(
        config, model, state_view=state_view if callable(state_view) else None
    )
    host.context.register(
        CONTEXT_NAME,
        director.contribution,
        # Model-produced and short-lived: a hint about tone, never an
        # instruction and never a source of facts.
        trust=Trust.DERIVED,
        priority=CONTEXT_PRIORITY,
        max_chars=MAX_RENDERED_CHARS,
    )
    host.observers.append(director)
    host.closers.append(director.close)

    if config.proposals_enabled:
        _wire_proposals(host, app_config, config, model)


def _wire_proposals(host, app_config, config: PersonaConfig, model) -> None:
    """Attach gated scans; auto mode writes only the managed example window."""
    db_path = getattr(app_config, "db_path", None)
    persona_path = getattr(app_config, "persona_path", None)
    if db_path is None or persona_path is None:
        log.error(
            "persona proposals are enabled but no database or persona path is "
            "available; skipping"
        )
        return

    from ...storage.conversation import ConversationStore
    from ...storage.database import SqliteDatabase
    from ...storage.persona_review import ProposalStore
    from .proposals import PersonaProposer
    from .queue import PersonaProposalQueue

    database = SqliteDatabase(db_path)
    conversations = ConversationStore(database)
    scopes = tuple(
        f"group:{group_id}"
        for group_id in sorted(getattr(app_config, "group_allowlist", ()) or ())
    )
    if not scopes:
        database.close()
        return
    queue = PersonaProposalQueue(
        ProposalStore(database),
        config,
        PersonaProposer(model),
        scopes,
        conversations.recent,
        # Read fresh each pass, never cached: a deployer who edits the file
        # should get suggestions against what it says now.
        lambda: Path(persona_path).read_text(encoding="utf-8"),
        persona_path=Path(persona_path),
        model=model,
        incremental_reader=conversations.after_id,
    )
    host.workers.append(queue.run)
    host.closers.append(database.close)


def validate() -> dict:
    config = PersonaConfig.from_env()
    return {
        "enabled": config.enabled,
        "ttl_minutes": config.ttl_minutes,
        "proposals_enabled": config.proposals_enabled,
    }
