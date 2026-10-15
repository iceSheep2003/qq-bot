from __future__ import annotations

from qqbot.agent.memory.wiki.page import WikiEntry, WikiLink, WikiPage, WikiRelation, WikiSlang, WikiTopic
from qqbot.agent.memory.wiki.writer import WikiWriter
from qqbot.infrastructure.storage.wiki_files import MarkdownWikiStore


def page() -> WikiPage:
    return WikiPage(
        page_id="groups/g1",
        scope="group",
        entity_id="_group_",
        title="测试群",
        summary="常聊系统。",
        entries=(WikiEntry("e1", "喜欢操作系统", topic="学习", source_event_id="m1", observed_at=10, confidence=0.8),),
        relations=(WikiRelation("r1", "bot", "u1", score=0.7, observed_at=10),),
        topics=(WikiTopic("t1", "操作系统", summary="常聊课程", mention_count=2, last_seen_at=10),),
        slang=(WikiSlang("开黑", "一起组队", confidence=0.9, last_seen_at=10),),
        links=(WikiLink("groups/g1", "groups/g1/members/u1", "frequent_member", "小明", confidence=0.9),),
        version="v1",
    )


def test_markdown_snapshot_round_trips_to_page(tmp_path):
    writer = WikiWriter()
    writer.write(tmp_path, page())

    loaded = MarkdownWikiStore(tmp_path).load()

    assert len(loaded) == 1
    restored = loaded[0]
    assert restored.page_id == "groups/g1"
    assert restored.entries[0].claim == "喜欢操作系统"
    assert restored.topics[0].name == "操作系统"
    assert restored.slang[0].term == "开黑"
    assert restored.links[0].target_page_id == "groups/g1/members/u1"
    assert restored.relations[0].score == 0.7
