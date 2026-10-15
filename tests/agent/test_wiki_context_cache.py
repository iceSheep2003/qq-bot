from __future__ import annotations

from qqbot.agent.memory.wiki.cache import WikiContextCache
from qqbot.agent.memory.wiki.entity import WikiEntity, WikiFact


def test_entity_is_small_and_can_select_facts_like_slang_records():
    entity = WikiEntity(
        scope="group_member",
        entity_id="u1",
        name="小明",
        summary="喜欢讨论操作系统。",
        facts=(
            WikiFact(
                fact_id="e1",
                text="正在准备操作系统课程",
                topic="学习",
                source_ids=("evt-1",),
                confidence=0.9,
            ),
        ),
        version="v1",
    )
    assert entity.preview() == {"scope": "group_member", "entity_id": "u1", "name": "小明", "summary": "喜欢讨论操作系统。", "version": "v1"}
    assert entity.select("操作系统")[0].text == "正在准备操作系统课程"


def test_cache_reuses_query_scoped_wiki_context_until_ttl():
    now = [100.0]
    calls: list[tuple[str, str, str]] = []
    cache = WikiContextCache(clock=lambda: now[0], ttl_seconds=30)

    def load(scope: str, subject: str, query: str):
        calls.append((scope, subject, query))
        return {"items": [f"{scope}:{subject}:{query}"]}

    assert cache.get_or_load("group:1", "u1", "黑话", "v1", load) == {
        "items": ["group:1:u1:黑话"]
    }
    assert cache.get_or_load("group:1", "u1", "黑话", "v1", load) == {
        "items": ["group:1:u1:黑话"]
    }
    assert calls == [("group:1", "u1", "黑话")]

    now[0] = 130.1
    cache.get_or_load("group:1", "u1", "黑话", "v1", load)
    assert len(calls) == 2


def test_cache_key_isolated_by_scope_subject_query_and_wiki_version():
    calls: list[tuple[str, str, str]] = []
    cache = WikiContextCache(clock=lambda: 10, ttl_seconds=300)

    def load(scope: str, subject: str, query: str):
        calls.append((scope, subject, query))
        return {"query": query}

    cache.get_or_load("group:1", "u1", "A", "v1", load)
    cache.get_or_load("group:2", "u1", "A", "v1", load)
    cache.get_or_load("group:1", "u2", "A", "v1", load)
    cache.get_or_load("group:1", "u1", "B", "v1", load)
    cache.get_or_load("group:1", "u1", "A", "v2", load)
    assert len(calls) == 5


def test_full_page_read_does_not_hit_selected_entry_cache():
    calls = [0]
    cache = WikiContextCache(clock=lambda: 10, ttl_seconds=300)

    def load(scope: str, subject: str, query: str):
        calls[0] += 1
        return {"mode": calls[0]}

    assert cache.get_or_load("group:1", "u1", "A", "v1", load, read_mode="selected") == {"mode": 1}
    assert cache.get_or_load("group:1", "u1", "A", "v1", load, read_mode="full") == {"mode": 2}


def test_failed_load_is_not_cached_and_scope_invalidation_is_bounded():
    cache = WikiContextCache(clock=lambda: 1, ttl_seconds=300)
    attempts = [0]

    def load(scope: str, subject: str, query: str):
        attempts[0] += 1
        if attempts[0] == 1:
            raise RuntimeError("temporary wiki read failure")
        return {"items": [query]}

    try:
        cache.get_or_load("group:1", "u1", "A", "v1", load)
    except RuntimeError:
        pass
    assert cache.get_or_load("group:1", "u1", "A", "v1", load) == {"items": ["A"]}

    cache.get_or_load("group:1", "u2", "B", "v1", load)
    cache.get_or_load("private:u1", "u1", "C", "v1", load)
    assert cache.invalidate_scope("group:1") == 2
    assert cache.size == 1
