"""Validated owner declarations layered over existing installed chat skills."""

from __future__ import annotations
import json
from pathlib import Path

from qunbot.contracts.extension_inputs import validate_skill
from qunbot.contracts.schema import ContractError
from qunbot.agent.skills.catalog import Skill
from qunbot.agent.skills.audience import ScopedSkills, audiences_for


class OwnerSkills:
    def __init__(self, root: Path, legacy, capabilities: frozenset[str]):
        self.legacy = legacy
        self._declarations = {}
        self._bodies = {}
        self.unavailable = []
        root = root.resolve()
        for declaration_path in sorted(root.glob("*/owner-skill.json")):
            if not declaration_path.resolve().is_relative_to(root):
                raise ContractError("Skill declaration escapes owner root")
            if declaration_path.stat().st_size > 16384:
                raise ContractError("Skill declaration too large")
            declaration = json.loads(declaration_path.read_text())
            validate_skill(declaration)
            name = declaration["name"]
            if name in self._declarations:
                raise ContractError("Duplicate owner skill identity")
            path = (
                declaration_path.parent / declaration["instructions_path"]
            ).resolve()
            if (
                not path.is_relative_to(declaration_path.parent.resolve())
                or not path.is_file()
            ):
                raise ContractError("Skill instructions escape local root")
            if path.stat().st_size > 65536:
                raise ContractError("Skill instructions too large")
            missing = set(declaration["required_capabilities"]) - capabilities
            if missing:
                self.unavailable.append(name)
                continue
            body = path.read_text(encoding="utf-8").strip()
            if not body:
                raise ContractError("Skill instructions empty")
            self._declarations[name] = declaration
            self._bodies[name] = body[: declaration["context_budget_chars"]]

    @property
    def skills(self):
        values = {
            s.name: s for s in self.legacy.skills if s.name not in self.unavailable
        }
        for name, d in self._declarations.items():
            values[name] = Skill(
                name,
                d["description"],
                (),
                self._bodies[name],
                d["priority"],
                d["activation"] == "always",
                audiences_for(d.get("audiences")),
            )
        return list(values.values())

    def for_audience(self, audience):
        return ScopedSkills(self, audience)

    def _visible(self, audience):
        return self.skills if audience is None else self.for_audience(audience).skills

    def by_name(self, name, *, audience=None):
        return next((s for s in self._visible(audience) if s.name == name), None)

    def select(self, text, *, proactive=False, audience=None):
        # New declarations are loaded through fixed skill_load on demand; no
        # hidden keyword that can install or execute anything.
        return self.legacy.select(
            text, proactive=proactive, audience=audience,
            exclude_names=frozenset(self._declarations) | frozenset(self.unavailable),
        )

    def stable_instructions(self, *, audience=None):
        pieces = []
        budget = 4000
        for s in sorted(self._visible(audience), key=lambda s: (-s.priority, s.name)):
            if s.always_on and len(s.body) <= budget:
                pieces.append(s.name + ":\n" + s.body)
                budget -= len(s.body)
        return "\n\n".join(pieces)

    def catalog_text(self, *, audience=None):
        return "\n".join(
            f"- {s.name}: {s.description[:300]}" for s in self._visible(audience) if not s.always_on
        )[:5000]

    def load(self, name, *, audience=None):
        skill = self.by_name(name, audience=audience)
        if not skill or name in self.unavailable:
            raise ContractError("Skill unavailable or not enabled")
        declaration = self._declarations.get(name)
        return {
            "skill_id": name,
            "version": declaration["version"]
            if declaration
            else "installed-instructions-v1",
            "instructions": skill.body[:12000],
            "required_capabilities": declaration["required_capabilities"]
            if declaration
            else [],
        }
