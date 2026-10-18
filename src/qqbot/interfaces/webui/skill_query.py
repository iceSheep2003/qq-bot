"""Read-only Skill catalog projection for the owner workbench."""
from __future__ import annotations


class SkillConsoleQuery:
    def __init__(self, catalog):
        self.catalog_source = catalog

    def catalog(self, calls=None):
        calls = calls or {}
        skills = []
        for skill in getattr(self.catalog_source, "skills", ()):
            skills.append({
                "name": skill.name,
                "description": skill.description,
                "triggers": list(skill.triggers),
                "priority": int(skill.priority),
                "always_on": bool(skill.always_on),
                "audiences": sorted(skill.audiences),
                "body_chars": len(skill.body),
                "body_preview": skill.body[:800],
                "calls": int(calls.get(skill.name, 0)),
            })
        skills.sort(key=lambda row: (-row["calls"], -row["priority"], row["name"]))
        unavailable = sorted(str(name) for name in getattr(self.catalog_source, "unavailable", ()))
        return {"skills": skills, "unavailable": unavailable, "total": len(skills)}
