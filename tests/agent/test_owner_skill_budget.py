"""Owner overrides and unavailable skills must not consume selection slots."""

import json

import pytest

from qunbot.agent.skills.catalog import SkillCatalog
from qunbot.agent.skills.host import OwnerSkills
from tests.agent.test_scene_refactor import skill


@pytest.mark.parametrize("audience", ["group", "private"])
@pytest.mark.parametrize("reason", ["override", "unavailable"])
def test_excluded_owner_skills_are_removed_before_selection_budget(tmp_path, audience, reason):
    for index in range(3):
        name = f"owner{index}"
        skill(tmp_path, name, f"[{audience}]", priority=100)
        (tmp_path / name / "instructions.md").write_text("owner body")
        (tmp_path / name / "owner-skill.json").write_text(json.dumps({
            "name": name, "version": "1", "description": "owner",
            "activation": "on_demand", "required_capabilities": ["not-installed"] if reason == "unavailable" else [],
            "instructions_path": "instructions.md", "context_budget_chars": 1000,
            "priority": 100, "audiences": ["private" if audience == "group" else "group"],
        }))
    skill(tmp_path, "z-visible", f"[{audience}]")
    catalog = OwnerSkills(tmp_path, SkillCatalog(tmp_path), frozenset())
    selected = catalog.for_audience(audience).select("学习计划")
    assert [s.name for s in selected] == ["z-visible"]
