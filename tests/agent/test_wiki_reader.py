from __future__ import annotations

from qqbot.agent.memory.wiki.page import WikiEntry, WikiPage, WikiSlang, WikiTopic
from qqbot.agent.memory.wiki.reader import WikiReader


def pages() -> tuple[WikiPage, ...]:
    return (
        WikiPage(
            page_id="groups/g1",
            scope="group",
            entity_id="_group_",
            title="测试群",
            summary="群里常聊操作系统。",
            entries=(
                WikiEntry("g-old", "以前聊数学", topic="学习", observed_at=10),
                WikiEntry("g-new", "最近聊操作系统", topic="学习", observed_at=20),
            ),
            topics=(WikiTopic("os", "操作系统", last_seen_at=20),),
            slang=(WikiSlang("开黑", "一起组队", confidence=0.9),),
            version="g-v1",
        ),
        WikiPage(
            page_id="groups/g1/members/u1",
            scope="group_member",
            entity_id="u1",
            title="小明",
            summary="喜欢操作系统话题。",
            entries=(WikiEntry("p1", "正在准备操作系统课程", topic="学习", observed_at=30),),
            version="p-v1",
        ),
    )


def test_selected_read_merges_group_and_person_without_reading_everything():
    result = WikiReader(pages()).read("group:g1", "u1", "学习", limit=2)

    assert result["mode"] == "selected"
    assert [item["entry_id"] for item in result["entries"]] == ["p1", "g-new"]
    assert "g-old" not in {item["entry_id"] for item in result["entries"]}
    assert result["pages"] == ["groups/g1", "groups/g1/members/u1"]
    assert result["slang"] == []


def test_full_read_is_explicit_and_preview_does_not_include_entries():
    reader = WikiReader(pages())
    preview = reader.read("group:g1", "u1", "学习", mode="preview")
    full = reader.read("group:g1", "u1", "学习", mode="full")

    assert preview["entries"] == []
    assert full["mode"] == "full"
    assert {item["entry_id"] for item in full["entries"]} == {"g-old", "g-new", "p1"}


def test_private_scope_cannot_fall_back_to_a_group_page():
    reader = WikiReader(pages())
    result = reader.read("private:u1", "u1", "操作系统")

    assert result["entries"] == []
    assert result["pages"] == []
