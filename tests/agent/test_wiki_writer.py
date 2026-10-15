from __future__ import annotations

from qqbot.agent.memory.wiki.page import WikiEntry, WikiLink, WikiPage, WikiSlang, WikiTopic
from qqbot.agent.memory.wiki.writer import WikiWriter


def page() -> WikiPage:
    return WikiPage(
        page_id="groups/g1",
        scope="group",
        entity_id="_group_",
        title="测试群",
        summary="常聊操作系统。",
        entries=(
            WikiEntry("old", "旧事实", observed_at=10, source_event_id="e0", confidence=0.7),
            WikiEntry("new", "新事实", observed_at=20, source_event_id="e1", confidence=0.9),
        ),
        topics=(WikiTopic("os", "操作系统", summary="群内常聊", last_seen_at=20),),
        slang=(WikiSlang("开黑", "一起组队", source_event_ids=("e1",), confidence=0.9),),
        links=(WikiLink("groups/g1", "groups/g1/members/u1", "frequent_member", "小明"),),
        version="v1",
    )


def test_writer_renders_stable_frontmatter_and_latest_first_entries():
    rendered = WikiWriter().render(page())

    assert rendered.startswith("---\n")
    assert 'title: "测试群"' in rendered
    assert 'version: "v1"' in rendered
    assert rendered.index("新事实") < rendered.index("旧事实")
    assert "source_event_id: e1" in rendered
    assert "## 主题" in rendered
    assert "## 黑话" in rendered
    assert "[小明](g1/members/u1.md)" in rendered


def test_writer_writes_only_inside_root_and_replaces_atomically(tmp_path):
    writer = WikiWriter()
    path = writer.write(tmp_path, page())

    assert path == tmp_path / "groups/g1.md"
    assert path.read_text(encoding="utf-8") == writer.render(page())
    assert not list(tmp_path.rglob("*.tmp"))
