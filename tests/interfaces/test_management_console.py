from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from qqbot.agent.memory.wiki.page import WikiEntry, WikiLink, WikiPage, WikiSlang, WikiTopic
from qqbot.infrastructure.storage.wiki_files import MarkdownWikiStore
from qqbot.agent.memory.wiki.writer import WikiWriter
from qqbot.interfaces.webui.dashboard import DashboardQuery
from qqbot.interfaces.webui.skill_query import SkillConsoleQuery


def test_skill_console_exposes_audience_triggers_and_actual_usage():
    skills = SimpleNamespace(skills=[
        SimpleNamespace(name="group-chat", description="群聊表达", triggers=("群", "聊天"), priority=4, always_on=True, audiences=frozenset({"group"}), body="body"),
        SimpleNamespace(name="private-chat", description="私聊表达", triggers=("私聊",), priority=3, always_on=False, audiences=frozenset({"private"}), body="body"),
    ], unavailable=["missing-skill"])
    result = SkillConsoleQuery(skills).catalog({"group-chat": 5, "private-chat": 2})
    assert result["skills"][0]["name"] == "group-chat"
    assert result["skills"][0]["audiences"] == ["group"]
    assert result["skills"][0]["calls"] == 5
    assert result["unavailable"] == ["missing-skill"]


def test_skill_console_empty_catalog_is_explicit():
    result = SkillConsoleQuery(SimpleNamespace(skills=[], unavailable=[])).catalog({})
    assert result == {"skills": [], "unavailable": [], "total": 0}


def _wiki(tmp_path: Path):
    root = tmp_path / "wiki"
    root.mkdir()
    page = WikiPage(
        "groups/1", "group", "1", "测试群",
        summary="群资料",
        entries=(WikiEntry("e1", "最近在讨论数据库", observed_at=20, confidence=.9),),
        topics=(WikiTopic("t1", "数据库", "SQL", mention_count=3, last_seen_at=20),),
        slang=(WikiSlang("db", "数据库", confidence=.8, last_seen_at=20),),
        links=(WikiLink("groups/1", "groups/1/members/7", label="小七"),),
        version="v2",
    )
    (root / "groups").mkdir()
    (root / "groups/1.md").write_text(WikiWriter().render(page), encoding="utf-8")
    return root


def test_wiki_management_lists_real_markdown_entities_and_fields(tmp_path):
    root = _wiki(tmp_path)
    query = DashboardQuery(None, MarkdownWikiStore(root))
    result = query.wiki_catalog()
    assert result["available"] is True
    assert result["pages"][0]["page_id"] == "groups/1"
    assert result["pages"][0]["facts"] == 1
    assert result["pages"][0]["topics"] == 1
    assert result["pages"][0]["slang"] == 1


def test_wiki_management_filters_scope_and_bounds(tmp_path):
    root = _wiki(tmp_path)
    query = DashboardQuery(None, MarkdownWikiStore(root))
    assert query.wiki_catalog(scope="group:1", limit=1)["pages"][0]["page_id"] == "groups/1"
    assert query.wiki_catalog(scope="group:missing")["pages"] == []
    try:
        query.wiki_catalog(limit=0)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid limit must be rejected")
