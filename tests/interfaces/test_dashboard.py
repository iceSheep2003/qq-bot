from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace
from pathlib import Path
import pytest
from qqbot.contracts.telemetry import trace_turn, record
from qqbot.infrastructure.storage.database import SqliteDatabase
from qqbot.infrastructure.storage.traces import TraceStore
from qqbot.interfaces.webui.dashboard import DashboardQuery
from qqbot.agent.memory.wiki.page import WikiPage, WikiLink

@pytest.fixture
def store(tmp_path):
    database = SqliteDatabase(tmp_path / "test.db")
    from qqbot.infrastructure.storage.traces import migrate
    migrate(database.db)
    yield TraceStore(database)
    database.close()

def event(id="evt", scope="group:1"):
    return SimpleNamespace(event_id=id, scope=scope, user_id="7", group_id="1", origin="message")

def seed(store, id="evt", scope="group:1", cached=40, prompt=100):
    with trace_turn(event(id, scope), store):
        record("admission", allowed=True)
        record("model", prompt_tokens=prompt, completion_tokens=20,
               cached_tokens=cached, duration_ms=12, attempts=1, prefix_hash="abc")
        record("delivery", outcome="sent")

def test_trace_persists_and_keeps_only_operational_evidence(store):
    with trace_turn(event(), store):
        record("context", skills=["private-chat"], prompt="secret", api_key="secret", content="secret")
        record("delivery", outcome="sent")
    rows=store.list()["items"]
    assert len(rows)==1
    row=store.detail(rows[0]["id"])
    assert row["event_id"]=="evt"
    assert row["outcome"]=="sent"
    assert row["steps"][0]["skills"]==["private-chat"]
    assert "secret" not in json.dumps(row)

def test_multicall_tokens_are_summed_and_unknown_is_explicit(store):
    with trace_turn(event(), store):
        record("model", prompt_tokens=100, completion_tokens=20, cached_tokens=50)
        record("model", prompt_tokens=300, completion_tokens=40, cached_tokens=0)
        record("model", prompt_tokens=10, completion_tokens=2, cached_tokens="unknown")
    stats=store.overview()
    assert stats["prompt_tokens"]==410
    assert stats["completion_tokens"]==62
    assert stats["model_calls"]==3
    assert stats["cache_hit_ratio"]==pytest.approx(.125)
    assert stats["cache_unknown_calls"]==1
    assert stats["cache_known_calls"]==2

def test_all_unknown_cache_is_not_zero(store):
    seed(store, cached="unknown")
    assert store.overview()["cache_hit_ratio"] is None

def test_empty_dashboard_has_no_fake_metrics(store):
    stats=store.overview()
    assert stats["turns"]==0
    assert stats["cache_hit_ratio"] is None
    assert stats["latency_p95_ms"] is None

def test_scope_and_cursor_never_mix_groups(store):
    seed(store,"a"); seed(store,"b","private:7"); seed(store,"c")
    first=store.list(scope="group:1",limit=1)
    assert first["items"][0]["event_id"]=="c"
    second=store.list(scope="group:1",limit=1,before=first["next_cursor"])
    assert [r["event_id"] for r in second["items"]]==["a"]
    assert store.overview(scope="private:7")["turns"]==1
    assert store.detail(99999) is None

def test_storage_survives_connection_restart(tmp_path):
    path=tmp_path/"persist.db"
    db=SqliteDatabase(path)
    from qqbot.infrastructure.storage.traces import migrate
    migrate(db.db)
    seed(TraceStore(db)); db.close()
    db=SqliteDatabase(path)
    assert TraceStore(db).list()["items"][0]["event_id"]=="evt"
    db.close()

@pytest.mark.parametrize("kwargs",[{"hours":0},{"hours":169},{"limit":0},{"limit":201},{"before":-1}])
def test_query_bounds(store,kwargs):
    with pytest.raises(ValueError): store.list(**kwargs)

def test_failure_records_class_without_exception_message(store):
    with pytest.raises(ValueError), trace_turn(event(),store):
        raise ValueError("provider-secret")
    row=store.detail(store.list()["items"][0]["id"])
    assert row["outcome"]=="error"
    assert row["steps"][-1]["error_kind"]=="ValueError"
    assert "provider-secret" not in json.dumps(row)

def test_telemetry_failure_cannot_break_conversation():
    class Broken:
        def save(self,trace): raise RuntimeError("db")
    with trace_turn(event(),Broken()): record("delivery",outcome="sent")

def test_concurrency_and_background_tasks_are_isolated(store):
    async def run():
        async def turn(id):
            with trace_turn(event(id),store):
                async def background(): record("model",prompt_tokens=999)
                await asyncio.create_task(background())
                await asyncio.sleep(0)
                record("model",prompt_tokens=10,completion_tokens=2,cached_tokens=1)
        await asyncio.gather(turn("a"),turn("b"))
    asyncio.run(run())
    assert store.overview()["prompt_tokens"]==20
    assert store.overview()["model_calls"]==2

def test_wiki_uses_real_edges_and_reports_dangling_links():
    pages=[WikiPage("group:1","group","1","群",links=(WikiLink("group:1","person:7"),WikiLink("group:1","missing"))),
           WikiPage("person:7","group_member","7","小七"), WikiPage("private:8","private","8","私聊")]
    query=DashboardQuery(None,SimpleNamespace(load=lambda:pages))
    graph=query.wiki_graph(scope="group:1")
    assert {n["id"] for n in graph["nodes"]}=={"group:1","person:7"}
    assert len(graph["links"])==1
    assert graph["dangling_links"]==1
    assert query.wiki_page("../../.env") is None
    assert query.wiki_page("person:7")["title"]=="小七"

def test_wiki_unavailable_is_distinct_from_empty():
    assert DashboardQuery(None).wiki_graph()["available"] is False
    assert DashboardQuery(None,SimpleNamespace(load=lambda:[])).wiki_graph()["available"] is True

def test_wiki_graph_is_bounded_and_marks_truncation():
    pages=[WikiPage(str(i),"group",str(i),str(i)) for i in range(4)]
    graph=DashboardQuery(None,SimpleNamespace(load=lambda:pages)).wiki_graph(limit=2)
    assert len(graph["nodes"])==2
    assert graph["total_nodes"]==4
    assert graph["truncated"] is True

def test_dashboard_assets_offer_real_navigation_and_safe_rendering():
    root=Path("src/qqbot/interfaces/webui/static")
    html=(root/"index.html").read_text()
    assert "/dashboard.js" in html
    js=(root/"dashboard.js").read_text() if (root/"dashboard.js").exists() else ""
    for endpoint in ("/api/dashboard/overview","/api/dashboard/traces","/api/dashboard/wiki"):
        assert endpoint in js
    assert "textContent" in js
    assert "运行总览" in js and "核心配置" in js
    assert (root/"advanced.html").exists()
