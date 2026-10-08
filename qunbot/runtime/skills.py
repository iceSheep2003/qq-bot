"""Instruction-only skills: frontmatter parsing, boundary-aware trigger match.

A skill is *text*, never code. There is no import, no subprocess and no
filesystem callback anywhere in this module: a ``SKILL.md`` can only put words
into either the stable prefix or the dynamic suffix. That is a structural
property — :class:`Skill` holds strings and nothing callable — and it is what
lets the bot load instructions written by a deployer without that becoming an
execution surface.

Selection used to be a plain substring test (``trigger in text``), which fails
in both directions: a bare ``@`` trigger fires on every mention-bearing message,
and an ASCII trigger like ``meme`` fires inside ``memento``. Matching here is:

``normalized``  NFKC + casefold + whitespace collapse, so full-width input and
                casing do not decide whether a skill loads.
``bounded``     ASCII and digit triggers match on word boundaries; CJK triggers
                match as substrings, because Chinese has no word delimiters and
                a boundary test would simply never fire.
``scored``      a skill's score is the total length of its matched triggers, so
                a specific trigger ("表情包") outranks a generic one ("@") rather
                than the declaration order deciding. Ties fall back to the order
                the catalogue is declared in, which keeps results stable.
``quiet``       triggers named ``proactive`` are the proactive-chat switch, not
                content triggers, and never match on their own text.
"""

from __future__ import annotations

import logging
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: How many skill bodies may enter one turn. Bounded so a long message full of
#: trigger words cannot pull the whole catalogue into the context window.
MAX_SELECTED = 3

#: The one trigger that is a mode switch rather than a keyword.
PROACTIVE_TRIGGER = "proactive"

#: Added to a ``proactive``-tagged skill's score on a proactive turn, so the
#: mode's own skill leads the list instead of competing on keyword length.
PROACTIVE_BONUS = 100

#: A trigger longer than this is almost certainly a pasted sentence, not a
#: keyword; matching it would be the same as matching the whole message.
MAX_TRIGGER_CHARS = 32

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_LIST_ITEM = re.compile(r"^\[(.*)\]$")
_TOKEN = re.compile(r"[0-9a-z]+")


def normalize(text: str) -> str:
    """NFKC, casefold and collapse whitespace.

    Group members type full-width punctuation, mixed case and stray spaces;
    none of that should change which instructions the bot loads.
    """
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"\s+", " ", folded).strip()


def _has_cjk(text: str) -> bool:
    return any("㐀" <= ch <= "鿿" or "豈" <= ch <= "﫿" for ch in text)


def trigger_score(trigger: str, haystack: str) -> int:
    """How strongly ``trigger`` matches ``haystack`` (already normalized).

    Zero means no match. A positive result is the trigger's length, so longer
    (more specific) triggers outrank shorter ones.
    """
    trigger = normalize(trigger)
    if not trigger or trigger == PROACTIVE_TRIGGER:
        return 0
    if _has_cjk(trigger):
        # Chinese has no word delimiters: substring is the only honest test.
        return len(trigger) if trigger in haystack else 0
    # ASCII/digit triggers: require word boundaries so "meme" does not fire on
    # "memento" and "@" does not fire on an email address or a stray at-sign
    # glued to a word.
    if not re.search(r"[0-9a-z]", trigger):
        return len(trigger) if trigger in haystack else 0
    pattern = r"(?<![0-9a-z])" + re.escape(trigger) + r"(?![0-9a-z])"
    return len(trigger) if re.search(pattern, haystack) else 0


@dataclass(frozen=True)
class Skill:
    """One ``SKILL.md``: a name, a one-line description and a body of text.

    Every field is a string or a tuple of strings. There is deliberately no
    handler, path or callable here — see the module docstring.
    """

    name: str
    description: str
    triggers: tuple[str, ...]
    body: str
    priority: int = 0
    always_on: bool = False

    def matches(self, text: str, *, proactive: bool = False) -> int:
        """Score this skill against already-normalized ``text``.

        A skill tagged ``proactive`` is selected on proactive turns whatever the
        text says; its other triggers still work as ordinary keywords, so a
        proactive turn can carry both the mode skill and a topic skill.
        """
        score = sum(trigger_score(t, text) for t in self.triggers)
        if proactive and PROACTIVE_TRIGGER in self.triggers:
            score += PROACTIVE_BONUS
        return score


def _split_frontmatter(raw: str) -> tuple[dict[str, str], str] | None:
    """(fields, body) for a well-formed SKILL.md, else ``None``."""
    match = _FRONTMATTER.match(raw)
    if not match:
        return None
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().casefold()] = value.strip()
    return fields, raw[match.end():]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_triggers(value: str) -> tuple[str, ...]:
    """Accept ``a, b``, ``a，b`` and ``[a, b]`` alike."""
    value = _unquote(value.strip())
    bracket = _LIST_ITEM.match(value)
    if bracket:
        value = bracket.group(1)
    parts = re.split(r"[,，]", value)
    seen: list[str] = []
    for part in parts:
        token = _unquote(part.strip())
        if not token or len(token) > MAX_TRIGGER_CHARS:
            if token:
                log.warning("Ignoring over-long skill trigger %r", token[:40])
            continue
        folded = normalize(token)
        if folded and folded not in seen:
            seen.append(folded)
    return tuple(seen)


def _selected_sections(body: str, names: tuple[str, ...]) -> str:
    """Take named sections from an installed upstream skill, unchanged."""
    lines = body.splitlines()
    selected: list[str] = []
    wanted = set(names)
    copying = False
    for line in lines:
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            if level == 2:
                copying = title in wanted
        if copying:
            selected.append(line)
    return "\n".join(selected).strip()


def _bot_adapter(path: Path, fields: dict[str, str], body: str) -> tuple[str, tuple[str, ...], str, int, bool] | None:
    """A local policy for using a third-party skill in this chat bot.

    Upstream files remain intact. The adapter can only select text already in
    SKILL.md; it cannot load scripts or arbitrary relative paths into a turn.
    """
    adapter_path = path.parent / "bot-adapter.json"
    if not adapter_path.exists():
        return None
    try:
        config = json.loads(adapter_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("adapter must be a JSON object")
        names = tuple(str(value) for value in config.get("sections", ()))
        triggers = config.get("triggers", ())
        if not isinstance(triggers, list):
            raise ValueError("adapter requires a trigger list")
        excerpts = _selected_sections(body, names) if names else ""
        if names and (not excerpts or any(f"## {name}" not in excerpts for name in names)):
            raise ValueError("adapter names a missing skill section")
        body_file = str(config.get("body_file") or "")
        if body_file:
            # No path traversal or executable asset: only a sibling Markdown
            # excerpt explicitly installed by the deployer may enter a prompt.
            if Path(body_file).name != body_file or not body_file.endswith(".md"):
                raise ValueError("adapter body_file must be a sibling Markdown file")
            supplemental = (path.parent / body_file).read_text(encoding="utf-8")
            excerpts = "\n\n".join(part for part in (excerpts, supplemental.strip()) if part)
        if not excerpts:
            raise ValueError("adapter has no selected text")
        maximum = max(300, min(6000, int(config.get("max_chars", 4000))))
        intro = str(config.get("intro", "")).strip()
        combined = f"{intro}\n\n{excerpts}".strip()
        if len(combined) > maximum:
            raise ValueError("adapter selection exceeds max_chars")
        description = str(config.get("description") or fields.get("description", "")).strip()
        trigger_text = ",".join(str(item) for item in triggers)
        priority = int(config.get("priority", 0))
        always_on = config.get("always_on", False)
        if not isinstance(always_on, bool):
            raise ValueError("always_on must be boolean")
        return description, _parse_triggers(trigger_text), combined, priority, always_on
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Invalid bot adapter %s: %s", adapter_path, exc)
        return None


class SkillCatalog:
    """Instruction-only skills; no arbitrary code execution.

    ``enabled_names`` is the deployer's allow-list, computed from the enabled
    extensions. ``None`` disables the filter (tests, and replays that want the
    whole catalogue).
    """

    def __init__(self, root: Path, enabled_names: frozenset[str] | None = None):
        self.root = root
        self.enabled_names = enabled_names
        self.skills: list[Skill] = []
        self.reload()

    def reload(self) -> None:
        skills: list[Skill] = []
        by_name: dict[str, Skill] = {}
        for path in sorted(self.root.glob("*/SKILL.md")):
            parsed = _split_frontmatter(path.read_text(encoding="utf-8"))
            if parsed is None:
                log.warning("Skill %s has no frontmatter block; skipping", path)
                continue
            fields, body = parsed
            name = _unquote(fields.get("name", "")) or path.parent.name
            if self.enabled_names is not None and name not in self.enabled_names:
                continue
            description = _unquote(fields.get("description", ""))
            if not description:
                log.warning("Skill %s has no description; skipping", path)
                continue
            if name in by_name:
                log.warning(
                    "Duplicate skill name %r at %s; keeping the first", name, path
                )
                continue
            try:
                priority = int(fields.get("priority", "0") or 0)
            except ValueError:
                priority = 0
            adapter = _bot_adapter(path, fields, body)
            always_on = False
            if adapter is not None:
                description, triggers, body, priority, always_on = adapter
            elif (path.parent / "bot-adapter.json").exists():
                # A broken adapter must not fall back to loading the entire
                # third-party skill, including its Codex-specific workflow.
                continue
            else:
                triggers = _parse_triggers(fields.get("triggers", ""))
            skill = Skill(
                name=name,
                description=description,
                triggers=triggers,
                body=body.strip(),
                priority=priority,
                always_on=always_on,
            )
            by_name[name] = skill
            skills.append(skill)
        self.skills = skills

    def by_name(self, name: str) -> Skill | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def select(self, text: str, *, proactive: bool = False) -> list[Skill]:
        """The skills whose triggers fire for this turn, most specific first.

        Pure text matching: no skill can cause a lookup, a call or a write.
        """
        haystack = normalize(text)
        order = {id(skill): index for index, skill in enumerate(self.skills)}
        scored: list[tuple[int, int, int, Skill]] = []
        for skill in self.skills:
            if skill.always_on:
                continue
            score = skill.matches(haystack, proactive=proactive)
            if score > 0:
                # priority first, then match strength, then declaration order —
                # so results are stable and a deployer can pin a skill ahead of
                # a longer-triggered one without renaming triggers.
                scored.append((-skill.priority, -score, order[id(skill)], skill))
        scored.sort(key=lambda item: item[:3])
        return [item[3] for item in scored[:MAX_SELECTED]]

    def catalog_text(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills if not s.always_on)

    def stable_instructions(self) -> str:
        """Operator-enabled, always-on guidance kept identical across turns."""
        return "\n\n".join(f"{s.name}:\n{s.body}" for s in self.skills if s.always_on)
