from __future__ import annotations

from qqbot.agent.memory.wiki.legacy import LegacyWikiProjector


def test_projection_separates_group_person_and_private_memory():
    pages = LegacyWikiProjector().project(
        memories=(
            {"id": 1, "scope": "group:1", "user_id": "_group_", "content": "群里常聊操作系统", "created_at": 10, "updated_at": 12, "confidence": 0.8, "status": "active", "fact_type": "fact", "mention_policy": "soft_echo", "source_event_id": "m1"},
            {"id": 2, "scope": "group:1", "user_id": "u1", "content": "小明在准备操作系统", "created_at": 20, "updated_at": 20, "confidence": 0.9, "status": "active", "fact_type": "preference", "mention_policy": "tone_only", "source_event_id": "m2", "persona_summary": "自然接住学习话题"},
            {"id": 3, "scope": "private:u1", "user_id": "u1", "content": "私聊中约定周末讨论", "created_at": 30, "updated_at": 30, "confidence": 0.9, "status": "active", "fact_type": "promise", "mention_policy": "direct", "source_event_id": "m3"},
        ),
    )

    assert {page.page_id for page in pages} == {"groups/1", "groups/1/members/u1", "private/u1"}
    person = next(page for page in pages if page.page_id == "groups/1/members/u1")
    assert person.entries[0].persona_hint == "自然接住学习话题"
    assert person.entries[0].observed_at == 20


def test_projection_keeps_topics_and_only_publishes_usable_slang():
    pages = LegacyWikiProjector().project(
        memories=(),
        topics=({"scope": "group:1", "canonical_name": "操作系统", "summary": "常聊课程", "mention_count": 4, "last_seen_at": 40},),
        slang=(
            {"scope": "group:1", "term": "开黑", "meaning": "一起组队", "confidence": 0.9, "last_seen": 40, "status": "candidate"},
            {"scope": "group:1", "term": "拒绝", "meaning": "", "confidence": 0.9, "last_seen": 40, "status": "candidate"},
            {"scope": "group:1", "term": "废弃", "meaning": "旧释义", "confidence": 0.9, "last_seen": 40, "status": "rejected"},
        ),
    )

    page = pages[0]
    assert page.topics[0].name == "操作系统"
    assert [item.term for item in page.slang] == ["开黑"]
