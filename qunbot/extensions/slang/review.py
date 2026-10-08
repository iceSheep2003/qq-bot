"""Local review: a deployer's file, never a group command.

The distinction is the whole safety model. Group members can chat; they cannot
approve the phrases the bot learns, because approval is an authority the bot
does not accept over chat. A reviewer edits a JSON file (or calls
``SlangStore.set_status`` from their own script) and the next scan applies it.

The file carries two things: status decisions (approve/reject/reset) and
meanings. A meaning written here is recorded as ``human``, which is what stops
a later inference pass from revising it. Writing ``null`` for a term releases
that override and hands the term back to automatic inference.

The file is re-read every scan, so a correction lands without a restart. A
malformed file keeps the last good decision set and logs loudly rather than
silently clearing an approval — "the bot quietly stopped understanding the
group" is a worse failure than a stale one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

APPROVE = "approve"
REJECT = "reject"
RESET = "reset"
ACTIONS = (APPROVE, REJECT, RESET)

# review action -> the status it writes
_STATUS_BY_ACTION = {APPROVE: "approved", REJECT: "rejected", RESET: "candidate"}


@dataclass(frozen=True)
class Review:
    """Overrides for one scan, split into a global set and per-scope sets."""

    global_actions: dict[str, frozenset[str]] = field(default_factory=dict)
    scoped_actions: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)
    # term -> meaning. A value of None is not "no meaning": it is the deployer
    # writing `null`, which hands the term back to automatic inference.
    global_meanings: dict[str, str | None] = field(default_factory=dict)
    scoped_meanings: dict[str, dict[str, str | None]] = field(default_factory=dict)

    def action_for(self, scope: str, term: str) -> str | None:
        """The reviewer's decision for ``term`` in ``scope``, or None."""
        scoped = self.scoped_actions.get(scope) or {}
        for action in ACTIONS:
            if term in scoped.get(action, frozenset()):
                return action
        for action in ACTIONS:
            if term in self.global_actions.get(action, frozenset()):
                return action
        return None

    def status_for(self, scope: str, term: str) -> str | None:
        action = self.action_for(scope, term)
        return _STATUS_BY_ACTION[action] if action else None

    def _meaning(self, scope: str, term: str) -> tuple[bool, str | None]:
        scoped = self.scoped_meanings.get(scope) or {}
        if term in scoped:
            return True, scoped[term]
        if term in self.global_meanings:
            return True, self.global_meanings[term]
        return False, None

    def has_meaning(self, scope: str, term: str) -> bool:
        return self._meaning(scope, term)[0]

    def meaning_for(self, scope: str, term: str) -> str | None:
        """The deployer's definition, or None when they set none."""
        return self._meaning(scope, term)[1]

    def releases(self, scope: str, term: str) -> bool:
        """True when the file writes ``null``: stop overriding, resume learning."""
        present, value = self._meaning(scope, term)
        return present and value is None

    def is_empty(self) -> bool:
        return not (
            self.global_actions
            or self.scoped_actions
            or self.global_meanings
            or self.scoped_meanings
        )


def _terms(value: object, where: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list of terms")
    return frozenset(str(item).strip() for item in value if str(item).strip())


def _meanings(value: object, where: str) -> dict[str, str | None]:
    """``{term: meaning}``. ``null`` is kept as None — it means "release"."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object of term -> meaning")
    out: dict[str, str | None] = {}
    for term, meaning in value.items():
        key = str(term).strip()
        if not key:
            continue
        out[key] = None if meaning is None else str(meaning).strip()
    return out


def parse_review(payload: object) -> Review:
    """Read a review document. Raises ValueError on a shape we cannot trust."""
    if payload is None:
        return Review()
    if not isinstance(payload, dict):
        raise ValueError("slang review file must be a JSON object")
    global_actions = {
        action: _terms(payload.get(action), action) for action in ACTIONS
    }
    scoped_actions: dict[str, dict[str, frozenset[str]]] = {}
    scoped_meanings: dict[str, dict[str, str | None]] = {}
    scopes = payload.get("scopes") or {}
    if not isinstance(scopes, dict):
        raise ValueError("slang review 'scopes' must be an object")
    for scope, block in scopes.items():
        if not isinstance(block, dict):
            raise ValueError(f"slang review scope {scope!r} must be an object")
        scoped_actions[str(scope)] = {
            action: _terms(block.get(action), f"scopes[{scope}].{action}")
            for action in ACTIONS
        }
        scoped_meanings[str(scope)] = _meanings(
            block.get("meanings"), f"scopes[{scope}].meanings"
        )
    return Review(
        global_actions,
        scoped_actions,
        _meanings(payload.get("meanings"), "meanings"),
        scoped_meanings,
    )


def load_review(path: Path) -> Review:
    """Parse the review file. A missing file means "nothing reviewed yet"."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return Review()
    return parse_review(json.loads(raw))


class ReviewFile:
    """mtime-cached view of the review file, safe to poll every scan."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._stamp: tuple[float, int] | None = None
        self._cached = Review()

    def current(self) -> Review:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime, stat.st_size)
        except OSError:
            if self._stamp is not None:
                self._stamp, self._cached = None, Review()
            return self._cached
        if stamp == self._stamp:
            return self._cached
        try:
            review = load_review(self.path)
        except (ValueError, json.JSONDecodeError):
            log.exception(
                "slang review file %s is malformed; keeping the previous decisions",
                self.path,
            )
            return self._cached
        self._stamp, self._cached = stamp, review
        return review
