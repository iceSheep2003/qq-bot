"""Skill management writes files, not behaviour.

The console must be able to say why a skill is missing: off by the deployer, off
because its extension is off, or broken on disk. These tests pin each of those
answers, and pin that every write lands where the loader already reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qqbot.agent.skills.catalog import SkillCatalog, read_disabled, write_disabled
from qqbot.interfaces.webui.skill_admin import SkillAdmin

DOCUMENT = """---
name: {name}
description: {description}
triggers: [{triggers}]
---

{body}
"""


def install(root: Path, name: str, *, description="示例 Skill", triggers="测试, 演示", body="按这里的规则回答。", extra=""):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        DOCUMENT.format(name=name, description=description, triggers=triggers, body=body) + extra,
        encoding="utf-8",
    )
    return folder


class Catalog:
    """Minimal stand-in: the allow-list and a reload counter."""

    def __init__(self, allowed=None):
        self.enabled_names = allowed
        self.reloads = 0

    def reload(self):
        self.reloads += 1


def admin(root: Path, allowed=None, calls=None):
    return SkillAdmin(root, catalog=Catalog(allowed), calls=calls or {})


def test_every_installed_skill_is_listed_with_the_reason_it_is_not_running(tmp_path):
    install(tmp_path, "running")
    install(tmp_path, "blocked")
    install(tmp_path, "switched-off")
    write_disabled(tmp_path, {"switched-off"})
    (tmp_path / "empty").mkdir()

    data = admin(tmp_path, allowed=frozenset({"running", "switched-off"})).overview()
    by_name = {row["name"]: row for row in data["skills"]}

    assert by_name["running"]["state"] == "enabled"
    assert by_name["blocked"]["state"] == "unavailable"
    assert "白名单" in by_name["blocked"]["reason"]
    assert by_name["switched-off"]["state"] == "disabled"
    assert by_name["empty"]["state"] == "broken"
    assert "没有 SKILL.md" in by_name["empty"]["reason"]
    assert data["counts"] == {"enabled": 1, "disabled": 1, "unavailable": 1, "broken": 1}


def test_a_skill_without_a_description_is_reported_as_broken(tmp_path):
    install(tmp_path, "mute", description="")

    row = admin(tmp_path, allowed=frozenset({"mute"})).overview()["skills"][0]

    assert row["state"] == "broken"
    assert "description" in row["reason"]


def test_duplicate_names_are_flagged_instead_of_silently_dropped(tmp_path):
    install(tmp_path, "first", extra="")
    (tmp_path / "first" / "SKILL.md").write_text(
        DOCUMENT.format(name="twin", description="一", triggers="a", body="x"), encoding="utf-8"
    )
    install(tmp_path, "second")
    (tmp_path / "second" / "SKILL.md").write_text(
        DOCUMENT.format(name="twin", description="二", triggers="b", body="y"), encoding="utf-8"
    )

    rows = admin(tmp_path, allowed=frozenset({"twin"})).overview()["skills"]

    assert [row["state"] for row in rows] == ["broken", "broken"]
    assert all("重名" in row["reason"] for row in rows)


def test_detail_returns_the_raw_file_and_the_adapter_that_overrides_it(tmp_path):
    folder = install(tmp_path, "adapted", body="## 对话规则\n按规则来。")
    (folder / "bot-adapter.json").write_text(
        json.dumps({"sections": ["对话规则"], "always_on": True, "max_chars": 2000}), encoding="utf-8"
    )

    detail = admin(tmp_path).detail("adapted")

    assert detail["adapter"] is True
    assert detail["adapter_config"]["sections"] == ["对话规则"]
    assert detail["always_on"] is True
    assert "## 对话规则" in detail["raw"]


def test_saving_rewrites_the_document_in_the_format_the_loader_reads(tmp_path):
    install(tmp_path, "editable")
    query = admin(tmp_path, allowed=frozenset({"editable"}))

    result = query.save(
        "editable",
        {"description": "改过的描述", "triggers": ["新词", "旧词"], "priority": 7,
         "audiences": ["group", "private"], "body": "新的正文。"},
    )

    assert result["reloaded"] is True
    text = (tmp_path / "editable" / "SKILL.md").read_text(encoding="utf-8")
    assert "description: 改过的描述" in text
    assert "triggers: [新词, 旧词]" in text
    assert "audiences: [group, private]" in text
    catalog = SkillCatalog(tmp_path)
    skill = catalog.by_name("editable")
    assert skill.description == "改过的描述"
    assert skill.triggers == ("新词", "旧词")
    assert skill.priority == 7
    assert skill.audiences == frozenset({"group", "private"})


def test_saving_refuses_to_orphan_the_section_an_adapter_depends_on(tmp_path):
    folder = install(tmp_path, "adapted", body="## 对话规则\n按规则来。")
    (folder / "bot-adapter.json").write_text(json.dumps({"sections": ["对话规则"]}), encoding="utf-8")

    with pytest.raises(ValueError, match="对话规则"):
        admin(tmp_path).save("adapted", {"description": "x", "body": "没有那个小节了"})


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"description": "", "body": "x"}, "description"),
        ({"description": "d", "body": ""}, "正文"),
        ({"description": "d", "body": "x", "audiences": ["everyone"]}, "作用域"),
        ({"description": "d", "body": "x", "priority": 500}, "priority"),
        ({"description": "d", "body": "x", "triggers": ["x" * 40]}, "触发词"),
    ],
)
def test_invalid_edits_are_refused_before_anything_is_written(tmp_path, payload, message):
    install(tmp_path, "guarded")
    before = (tmp_path / "guarded" / "SKILL.md").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        admin(tmp_path).save("guarded", payload)

    assert (tmp_path / "guarded" / "SKILL.md").read_text(encoding="utf-8") == before


def test_creating_a_skill_writes_a_loadable_document(tmp_path):
    query = admin(tmp_path)

    result = query.create(
        {"name": "new-skill", "description": "新建的", "triggers": "甲, 乙", "body": "正文内容。"}
    )

    assert result["created"] == "new-skill"
    assert SkillCatalog(tmp_path).by_name("new-skill").triggers == ("甲", "乙")


@pytest.mark.parametrize("name", ["", "A大写", "带空格 的名字", "../escape", "-leading"])
def test_creating_rejects_names_that_would_escape_the_skills_directory(tmp_path, name):
    with pytest.raises(ValueError, match="名称"):
        admin(tmp_path).create({"name": name, "description": "d", "body": "x"})


def test_creating_refuses_to_overwrite_an_existing_directory(tmp_path):
    install(tmp_path, "taken")

    with pytest.raises(ValueError, match="已存在"):
        admin(tmp_path).create({"name": "taken", "description": "d", "body": "x"})


def test_importing_a_file_keeps_the_author_formatting_and_derives_the_name(tmp_path):
    raw = "---\nname: uploaded\ndescription: 上传的\ntriggers: [导入]\n---\n\n原始正文，保留换行。\n"

    result = admin(tmp_path).import_document("whatever.md", raw)

    assert result["created"] == "uploaded"
    assert (tmp_path / "uploaded" / "SKILL.md").read_text(encoding="utf-8") == raw


def test_importing_falls_back_to_the_filename_when_frontmatter_has_no_name(tmp_path):
    raw = "---\ndescription: 无名字\n---\n\n正文。\n"

    assert admin(tmp_path).import_document("My Cool Skill.md", raw)["created"] == "my-cool-skill"


@pytest.mark.parametrize(
    "raw, message",
    [
        ("没有 frontmatter", "frontmatter"),
        ("---\nname: xx\n---\n正文", "description"),
        ("   ", "空"),
    ],
)
def test_importing_refuses_documents_the_loader_would_skip(tmp_path, raw, message):
    with pytest.raises(ValueError, match=message):
        admin(tmp_path).import_document("x.md", raw)


def test_switching_a_skill_off_is_persisted_where_the_catalogue_reads_it(tmp_path):
    install(tmp_path, "voice")
    query = admin(tmp_path, allowed=frozenset({"voice"}))

    result = query.set_enabled("voice", False)

    assert result["enabled"] is False
    assert read_disabled(tmp_path) == frozenset({"voice"})
    assert SkillCatalog(tmp_path).by_name("voice") is None
    assert query.set_enabled("voice", True)["enabled"] is True
    assert SkillCatalog(tmp_path).by_name("voice") is not None


def test_switching_a_blocked_skill_on_says_it_will_not_take_effect(tmp_path):
    install(tmp_path, "blocked")
    query = admin(tmp_path, allowed=frozenset())

    result = query.set_enabled("blocked", True)

    assert "扩展" in result["runtime_effect"]
    assert SkillCatalog(tmp_path, frozenset()).by_name("blocked") is None


def test_archiving_moves_the_directory_instead_of_deleting_it(tmp_path):
    install(tmp_path, "old")
    write_disabled(tmp_path, {"old"})

    result = admin(tmp_path).archive("old")

    assert not (tmp_path / "old").exists()
    archived = tmp_path / ".archive" / result["archived"]
    assert (archived / "SKILL.md").is_file()
    assert read_disabled(tmp_path) == frozenset()
    assert admin(tmp_path).overview()["archived"][0]["name"] == result["archived"]


def test_unknown_skill_names_raise_key_error_so_the_route_can_answer_404(tmp_path):
    query = admin(tmp_path)

    assert query.detail("nope") is None
    with pytest.raises(KeyError):
        query.save("nope", {"description": "d", "body": "x"})
    with pytest.raises(KeyError):
        query.set_enabled("nope", True)
    with pytest.raises(KeyError):
        query.archive("nope")


def test_the_catalogue_honours_always_on_from_frontmatter(tmp_path):
    (tmp_path / "always").mkdir()
    (tmp_path / "always" / "SKILL.md").write_text(
        "---\nname: always\ndescription: 常驻\nalways_on: true\n---\n\n常驻正文。\n", encoding="utf-8"
    )

    skill = SkillCatalog(tmp_path).by_name("always")

    assert skill.always_on is True
    assert "always:" in SkillCatalog(tmp_path).stable_instructions()


def test_a_corrupt_state_file_leaves_every_skill_enabled(tmp_path):
    install(tmp_path, "safe")
    (tmp_path / ".state.json").write_text("{not json", encoding="utf-8")

    assert read_disabled(tmp_path) == frozenset()
    assert SkillCatalog(tmp_path).by_name("safe") is not None


def test_an_unavailable_skill_names_the_extension_that_owns_it(tmp_path):
    install(tmp_path, "voice")
    query = SkillAdmin(tmp_path, catalog=Catalog(frozenset()), extensions={"voice": "voice"})

    row = query.overview()["skills"][0]

    assert row["extension"] == "voice"
    assert "voice" in row["reason"]


def test_an_invalid_audience_makes_the_skill_broken_instead_of_guessing(tmp_path):
    (tmp_path / "odd").mkdir()
    (tmp_path / "odd" / "SKILL.md").write_text(
        "---\nname: odd\ndescription: 作用域写错了\naudiences: [everyone]\n---\n\n正文\n", encoding="utf-8"
    )

    row = admin(tmp_path).overview()["skills"][0]

    assert row["state"] == "broken"
    assert "作用域" in row["reason"]


def test_a_corrupt_adapter_is_reported_but_the_skill_stays_listed(tmp_path):
    folder = install(tmp_path, "adapted")
    (folder / "bot-adapter.json").write_text("{not json", encoding="utf-8")

    row = admin(tmp_path, allowed=frozenset({"adapted"})).overview()["skills"][0]

    assert row["adapter"] is True
    assert row["name"] == "adapted"


def test_detail_of_a_broken_skill_still_opens_with_an_empty_document(tmp_path):
    (tmp_path / "empty").mkdir()

    detail = admin(tmp_path).detail("empty")

    assert detail["state"] == "broken"
    assert detail["raw"] == ""
    assert detail["body"] == ""


def test_a_broken_skill_cannot_be_switched_on_until_the_file_is_fixed(tmp_path):
    (tmp_path / "empty").mkdir()
    query = admin(tmp_path)

    result = query.set_enabled("empty", True)

    assert "修好" in result["runtime_effect"]


def test_priority_must_be_a_number(tmp_path):
    install(tmp_path, "guarded")

    with pytest.raises(ValueError, match="priority"):
        admin(tmp_path).save("guarded", {"description": "d", "body": "x", "priority": "high"})


def test_creating_requires_a_description(tmp_path):
    with pytest.raises(ValueError, match="description"):
        admin(tmp_path).create({"name": "fresh", "body": "x"})


def test_importing_a_file_whose_name_cannot_be_derived_is_refused(tmp_path):
    with pytest.raises(ValueError, match="name"):
        admin(tmp_path).import_document("!!!.md", "---\ndescription: 无名字\n---\n正文")


def test_the_archive_list_is_bounded_and_newest_first(tmp_path):
    install(tmp_path, "first")
    query = admin(tmp_path)
    query.archive("first")
    install(tmp_path, "second")
    query.archive("second")

    archived = admin(tmp_path).overview()["archived"]

    assert [item["name"].split("-")[0] for item in archived] == ["second", "first"]
