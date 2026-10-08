"""Editing the deployer's slang review file from the owner console.

The console writes the review *file*. It does not become a second source of
truth, and it does not gain a write path into any bot subsystem: the learning
feature reads its decisions from that file and nowhere else, so a change made
here is indistinguishable from a deployer editing it by hand. That is the
point — review is an authority that belongs to whoever deployed the bot, and
this is that authority exercised with a mouse instead of an editor.

Two deliberate refusals:

* **A malformed file is not overwritten.** The reader keeps the last good
  decisions when the file will not parse, so writing over one would look
  exactly like the console silently wiping every approval.
* **This module knows the file format, not the feature.** It is the only place
  the console touches a review document, and it imports nothing from the
  package that consumes it — the console stays as self-contained as the rest
  of its read model.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

#: The three decisions a reviewer can record. A term's status is derived from
#: which list it appears in, so it belongs to at most one of them.
ACTIONS = ("approve", "reject", "reset")


class ReviewFormatError(ValueError):
    """The file on disk is not a review document we would be willing to edit."""


class ReviewFileEditor:
    """Read, merge and atomically rewrite one review file."""

    def __init__(self, path: Path):
        self.path = Path(path)

    # ------------------------------------------------------------- reading

    def load(self) -> dict:
        """The document as it stands. A missing file reads as "nothing yet"."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise ReviewFormatError(f"无法读取审查文件：{exc}") from exc
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReviewFormatError(f"审查文件不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise ReviewFormatError("审查文件的顶层必须是一个对象")
        return payload

    def summary(self) -> dict:
        """What the console shows: the file, parsed and counted."""
        payload = self.load()
        return {
            "path": str(self.path),
            "payload": payload,
            "actions": ACTIONS,
            "counts": _counts(payload),
        }

    # ------------------------------------------------------------- writing

    def set_decision(self, term: str, action: str, *, scope: str | None = None) -> dict:
        term = _term(term)
        if action not in ACTIONS:
            raise ValueError(f"未知的处置：{action}")
        payload = self.load()
        block = _block(payload, scope)
        for name in ACTIONS:
            terms = _terms(block, name)
            if name == action:
                if term not in terms:
                    terms.append(term)
            elif term in terms:
                terms = [item for item in terms if item != term]
            if terms:
                block[name] = terms
            else:
                block.pop(name, None)
        self._save(payload)
        return payload

    def set_meaning(self, term: str, meaning: str | None, *, scope: str | None = None) -> dict:
        """Record a definition, clear it, or hand the term back to learning.

        ``None`` writes ``null``, which the reader treats as "stop overriding
        this term" — the only way to undo a correction from the console.
        """
        term = _term(term)
        payload = self.load()
        block = _block(payload, scope)
        meanings = _meanings(block)
        if meaning is None:
            meanings[term] = None
        else:
            text = str(meaning).strip()
            if text:
                meanings[term] = text
            else:
                meanings.pop(term, None)
        if meanings:
            block["meanings"] = meanings
        else:
            block.pop("meanings", None)
        self._save(payload)
        return payload

    def forget(self, term: str, *, scope: str | None = None) -> dict:
        """Remove every mention of a term, so it goes back to being untouched."""
        term = _term(term)
        payload = self.load()
        scopes = payload.get("scopes")
        targets = (
            [scope] if scope else [None, *(scopes if isinstance(scopes, dict) else ())]
        )
        for target in targets:
            try:
                block = _block(payload, target)
            except ReviewFormatError:
                continue
            for name in ACTIONS:
                terms = [item for item in _terms(block, name) if item != term]
                if terms:
                    block[name] = terms
                else:
                    block.pop(name, None)
            meanings = _meanings(block)
            meanings.pop(term, None)
            if meanings:
                block["meanings"] = meanings
            else:
                block.pop("meanings", None)
        self._prune_empty_scopes(payload)
        self._save(payload)
        return payload

    @staticmethod
    def _prune_empty_scopes(payload: dict) -> None:
        """Drop group blocks that no longer say anything."""
        scopes = payload.get("scopes")
        if not isinstance(scopes, dict):
            return
        for key in [key for key, block in scopes.items() if block == {}]:
            scopes.pop(key, None)
        if not scopes:
            payload.pop("scopes", None)

    def _save(self, payload: dict) -> None:
        """Write it back in one step: a reader either sees the old file or the
        new one, never a half-written document that fails to parse."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise


def _term(value: object) -> str:
    term = str(value or "").strip()
    if not term:
        raise ValueError("词条不能为空")
    return term


def _block(payload: dict, scope: str | None) -> dict:
    """The mapping a change lands in: the top level, or one group's block."""
    if not scope:
        return payload
    scopes = payload.setdefault("scopes", {})
    if not isinstance(scopes, dict):
        raise ReviewFormatError("scopes 必须是一个对象")
    block = scopes.setdefault(str(scope), {})
    if not isinstance(block, dict):
        raise ReviewFormatError(f"scopes[{scope}] 必须是一个对象")
    return block


def _terms(block: dict, action: str) -> list:
    value = block.get(action)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReviewFormatError(f"{action} 必须是一个数组")
    return [str(item) for item in value]


def _meanings(block: dict) -> dict:
    value = block.get("meanings")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReviewFormatError("meanings 必须是一个对象")
    return dict(value)


def _counts(payload: dict) -> dict:
    """How many decisions the file records, top level and total."""
    total = {name: 0 for name in ACTIONS}
    total["meanings"] = 0
    blocks = [payload]
    scopes = payload.get("scopes")
    if isinstance(scopes, dict):
        blocks.extend(block for block in scopes.values() if isinstance(block, dict))
    for block in blocks:
        for name in ACTIONS:
            value = block.get(name)
            if isinstance(value, list):
                total[name] += len(value)
        value = block.get("meanings")
        if isinstance(value, dict):
            total["meanings"] += len(value)
    return total
