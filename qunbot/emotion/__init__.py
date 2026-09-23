"""The bot's own mood: a self-contained bounded context.

Nothing outside this package needs to know how a mood is stored, decayed or
worded. The application touches exactly three names — ``EmotionConfig``,
``build_emotion`` and the object that comes back — plus the ``MoodObserver``
port in ``qunbot/ports.py``.

Deleting the feature is deleting this directory and the handful of wiring
lines in ``qunbot/app.py`` and ``qunbot/service.py``.

The ownership boundary
----------------------

This package owns exactly one thing: **the bot's own state**. Three rules, all
of them enforced by ``tests/test_emotion.py`` rather than merely intended:

* It reads no group member's affection and writes none. The affection,
  relationship and profile tables belong to ``qunbot/relationships`` and are
  not imported here at all — this package imports ``domain``, ``ports`` and its
  own modules, nothing else.
* It never reads, writes or regenerates the stable persona. ``config/persona.md``
  is ``runtime/agent.py``'s input; the mood only ever contributes a line to the
  *dynamic* prompt suffix, under the ``mood`` name, at priority 60 — a
  lower-numbered, longer-lived contribution than persona's 65, because the
  state is the cause and the phrasing is re-derivable.
* The signal it takes in is the turn (what was said, and what the bot answered)
  as the situation the bot reacted to. Nothing about *who* the speaker is
  reaches the state except through that situation, and even that only ever
  lands in the short ``reason`` string the model is told not to quote.

What it deliberately is not
---------------------------

Weather, wall-clock time and memory replay are **not** part of this package.
They are separate, individually disableable contributors in
``qunbot/extensions/world_context``, so a mood can run with no network and a
clock can run with the mood switched off. Time enters this package only as the
``clock`` seam used for decay; no world facts do.

Duplicate delivery
------------------

``observe`` is at-most-once on its own, independently of the caller: the
conversation service already deduplicates post-reply work by event id, and this
package keeps its own ledger (``mood_observed``) so a direct second call with
the same event changes nothing. The claim is made inside the same transaction
as the write, so a *failed* assessment leaves the key free and a genuine retry
still applies.
"""

from .config import EmotionConfig
from .state import (
    BASELINE,
    DIMENSIONS,
    EmotionPolicy,
    Mood,
    MoodEvent,
    MoodSnapshot,
    loop_gain,
    replay,
    timeline,
)
from .system import EmotionSystem, build_emotion

__all__ = [
    "BASELINE",
    "DIMENSIONS",
    "EmotionConfig",
    "EmotionPolicy",
    "EmotionSystem",
    "Mood",
    "MoodEvent",
    "MoodSnapshot",
    "build_emotion",
    "loop_gain",
    "replay",
    "timeline",
]
