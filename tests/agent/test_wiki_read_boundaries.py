"""Private ownership and per-request budgets survive warm Wiki caches."""

import pytest

from qqbot.agent.memory.wiki.page import WikiEntry, WikiPage
from qqbot.agent.memory.wiki.reader import WikiReader


def reader():
    return WikiReader((WikiPage(
        page_id="private/7", scope="private", entity_id="7", title="私聊资料",
        entries=tuple(WikiEntry(str(i), f"学习计划{i}", observed_at=i) for i in range(5)),
    ),))


@pytest.mark.parametrize("mode", ["preview", "selected", "full"])
def test_private_page_requires_scope_owner_even_with_a_warm_cache(mode):
    wiki = reader()
    assert wiki.read("private:7", "7", "学习", mode=mode)["pages"] == ["private/7"]
    denied = wiki.read("private:8", "7", "学习", mode=mode)
    assert denied["pages"] == []
    assert denied["previews"] == []
    assert denied["entries"] == []


@pytest.mark.parametrize("limits", [(4, 1), (1, 4), (4, 0)])
def test_selected_cache_respects_each_requests_limit(limits):
    wiki = reader()
    for limit in limits:
        result = wiki.read("private:7", "7", "学习", limit=limit)
        assert len(result["entries"]) == limit
        assert [item["entry_id"] for item in result["entries"]] == list(reversed([str(i) for i in range(5)]))[:limit]


def test_page_identity_must_match_its_subject():
    wiki = WikiReader((WikiPage(
        page_id="private/7", scope="private", entity_id="8", title="错配页面",
        entries=(WikiEntry("a", "学习计划"),),
    ),))
    assert wiki.read("private:7", "7", "学习", mode="full")["pages"] == []
