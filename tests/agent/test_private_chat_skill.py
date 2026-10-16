"""The deployed default catalog provides audience-specific conversation guidance."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from qqbot.agent.skills.catalog import SkillCatalog
from qqbot.contracts.schema import ContractError
from qqbot.features.loader import enabled_skills


def catalog():
    root = Path(__file__).resolve().parents[2]
    return SkillCatalog(root / "skills", enabled_skills(SimpleNamespace(extensions=(), private_enabled=True)))


def test_default_catalog_has_private_conversation_guidance():
    skill = catalog().for_audience("private").by_name("private-chat")
    assert skill is not None
    assert skill.always_on
    assert "当前私聊" in skill.body


def test_private_guidance_is_in_the_private_stable_prefix_only():
    skills = catalog()
    assert "private-chat:" in skills.for_audience("private").stable_instructions()
    assert "private-chat:" not in skills.for_audience("group").stable_instructions()


def test_private_guidance_does_not_grant_group_or_memory_permissions():
    private = catalog().for_audience("private")
    assert private.by_name("private-chat") is not None
    with pytest.raises(ContractError):
        private.load("group-chat")
    body = private.load("private-chat")["instructions"]
    assert "授权" in body
    assert "定时任务" in body
