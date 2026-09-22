from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    triggers: tuple[str, ...]
    body: str


class SkillCatalog:
    """Instruction-only skills; no arbitrary code execution."""

    def __init__(self, root: Path, enabled_names: frozenset[str] | None = None):
        self.root = root
        self.enabled_names = enabled_names
        self.skills: list[Skill] = []
        self.reload()

    def reload(self) -> None:
        self.skills = []
        for path in sorted(self.root.glob("*/SKILL.md")):
            raw = path.read_text(encoding="utf-8")
            if not raw.startswith("---\n"):
                continue
            _, front, body = raw.split("---", 2)
            fields: dict[str, str] = {}
            for line in front.splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    fields[key.strip()] = value.strip().strip('"')
            name = fields.get("name", path.parent.name)
            if self.enabled_names is not None and name not in self.enabled_names:
                continue
            description = fields.get("description", "")
            if not description:
                continue
            triggers = tuple(
                x.strip().lower()
                for x in fields.get("triggers", "").split(",")
                if x.strip()
            )
            self.skills.append(Skill(name, description, triggers, body.strip()))

    def select(self, text: str, *, proactive: bool = False) -> list[Skill]:
        lowered = text.lower()
        return [
            s
            for s in self.skills
            if (proactive and "proactive" in s.triggers)
            or any(t != "proactive" and t in lowered for t in s.triggers)
        ][:3]

    def catalog_text(self) -> str:
        return "\n".join(f"- {s.name}: {s.description}" for s in self.skills)
