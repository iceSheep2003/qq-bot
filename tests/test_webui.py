from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from qunbot.extensions.webui.config import WebUIConfig
from qunbot.extensions.webui.envfile import EnvConfigStore
from qunbot.extensions.webui.schema import BY_KEY, validate_value
from qunbot.extensions.webui.server import WebUIServer


def test_env_store_masks_secrets_and_preserves_unrelated_lines(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# keep me\nBOT_MODEL_API_KEY=super-secret\nUNRELATED=yes\nBOT_PRIVATE_ENABLED=false\n",
        encoding="utf-8",
    )
    store = EnvConfigStore(path)
    values = store.read()
    assert values["BOT_MODEL_API_KEY"] == {
        "value": "",
        "configured": True,
        "secret": True,
    }
    assert store.update({"BOT_PRIVATE_ENABLED": True, "BOT_MODEL_API_KEY": ""}) == [
        "BOT_PRIVATE_ENABLED"
    ]
    saved = path.read_text(encoding="utf-8")
    assert "# keep me" in saved
    assert "UNRELATED=yes" in saved
    assert "BOT_MODEL_API_KEY=super-secret" in saved
    assert "BOT_PRIVATE_ENABLED=true" in saved


def test_env_store_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown settings"):
        EnvConfigStore(tmp_path / ".env").update({"EVIL_KEY": "value"})


def test_env_store_shows_documented_non_secret_defaults(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / ".env.example").write_text(
        "BOT_MEMORY_EXTRACT_EVERY=8\nBOT_MODEL_API_KEY=not-a-real-key\n",
        encoding="utf-8",
    )
    values = EnvConfigStore(tmp_path / ".env").read()
    assert values["BOT_MEMORY_EXTRACT_EVERY"]["value"] == "8"
    assert values["BOT_MODEL_API_KEY"]["value"] == ""
    assert values["BOT_MODEL_API_KEY"]["configured"] is False


def test_schema_validates_ranges_and_urls() -> None:
    assert validate_value(BY_KEY["BOT_REPLY_POLICY_THRESHOLD"], "0.7") == "0.7"
    with pytest.raises(ValueError):
        validate_value(BY_KEY["BOT_REPLY_POLICY_THRESHOLD"], "1.2")
    with pytest.raises(ValueError):
        validate_value(BY_KEY["BOT_MODEL_BASE_URL"], "javascript:alert(1)")


def test_style_allowlist_label_says_what_it_actually_controls() -> None:
    setting = BY_KEY["BOT_STYLE_ECHO_ALLOWED_USERS"]
    assert setting.label == "指定用户风格采样名单"
    assert "留空时学习群聊整体节奏" in setting.help


def test_markdown_guide_is_task_oriented_and_covers_every_module() -> None:
    guide = (Path(__file__).parents[1] / "qunbot/extensions/webui/docs/GUIDE.md").read_text(
        encoding="utf-8"
    )
    assert guide.startswith("# QunBot 操作手册")
    assert "[TOC]" in guide
    for task in (
        "接入 NapCat 和模型",
        "控制 Bot 什么时候回复",
        "配置记忆、好感度和人格",
        "配置主动发言和定时任务",
        "使用群管理功能",
        "配置表情包、语音和 QQ 频道",
        "管理扩展和系统参数",
    ):
        assert f"## {task}" in guide
    assert guide.count("### 实现说明") == 7
    assert "### 验证" in guide
    assert "### 任务没有执行" in guide


def test_markdown_guide_renders_navigation_and_real_sections() -> None:
    import markdown

    guide = (Path(__file__).parents[1] / "qunbot/extensions/webui/docs/GUIDE.md").read_text(
        encoding="utf-8"
    )
    rendered = markdown.markdown(
        guide,
        extensions=("extra", "sane_lists", "toc"),
        extension_configs={"toc": {"toc_depth": "2-3"}},
    )
    assert '<div class="toc">' in rendered
    assert "控制 Bot 什么时候回复" in rendered
    assert "qunbot/runtime/service.py" in rendered


def _request(url: str, token: str = "", method: str = "GET", body=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=2) as response:
        return response.status, json.loads(response.read())


def test_http_api_requires_auth_and_updates_allowlisted_config(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("BOT_PRIVATE_ENABLED=false\n", encoding="utf-8")
    server = WebUIServer(WebUIConfig(True, "127.0.0.1", 0, "test-token", env))
    handler = server._handler()
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            _request(base + "/api/config")
        assert denied.value.code == 401
        status, result = _request(
            base + "/api/config",
            "test-token",
            "PUT",
            {"changes": {"BOT_PRIVATE_ENABLED": "true"}},
        )
        assert status == 200
        assert result["restart_required"] is True
        assert env.read_text(encoding="utf-8") == "BOT_PRIVATE_ENABLED=true\n"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_memory_dashboard_endpoints_are_owner_only_and_json_stable(tmp_path: Path) -> None:
    class Query:
        def overview(self, scope=None): return {"scope": scope, "scopes": ["group:42"], "memories": {"total": 1}, "people": 2, "slang": 3}
        def graph(self, scope=None, limit=100): return {"nodes": [{"id": "bot", "kind": "bot", "label": "Bot"}], "links": []}

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    server = WebUIServer(WebUIConfig(True, "127.0.0.1", 0, "test-token", env))
    server.memory_query = Query()
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            _request(base + "/api/memory/overview")
        assert denied.value.code == 401
        status, body = _request(
            base + "/api/memory/overview?scope=group%3A42", "test-token"
        )
        assert status == 200
        assert body["data"]["scope"] == "group:42"
        _, graph = _request(base + "/api/memory/graph", "test-token")
        assert graph["data"]["nodes"][0]["kind"] == "bot"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_the_review_queue_is_owner_only_and_writes_the_file_the_feature_reads(
    tmp_path: Path,
) -> None:
    """One write surface, and it edits the deployer's file rather than a copy.

    Whatever the console writes has to be something the learning feature would
    accept, so the assertion that matters is the round-trip through its parser.
    """
    from qunbot.extensions.slang.review import load_review

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    review_path = tmp_path / "config" / "slang_review.json"
    server = WebUIServer(
        WebUIConfig(True, "127.0.0.1", 0, "test-token", env, review_path)
    )
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            _request(base + "/api/review/slang")
        assert denied.value.code == 401

        status, body = _request(base + "/api/review/slang", "test-token")
        assert status == 200
        assert body["data"]["payload"] == {}

        status, _ = _request(
            base + "/api/review/decision",
            "test-token",
            "POST",
            {"term": "上大分", "action": "approve"},
        )
        assert status == 200
        status, _ = _request(
            base + "/api/review/meaning",
            "test-token",
            "POST",
            {"term": "上大分", "meaning": "赢了、拿到好处"},
        )
        assert status == 200

        review = load_review(review_path)
        assert review.status_for("group:42", "上大分") == "approved"
        assert review.meaning_for("group:42", "上大分") == "赢了、拿到好处"

        # A malformed file is a conflict, not a silent repair.
        review_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(urllib.error.HTTPError) as broken:
            _request(
                base + "/api/review/decision",
                "test-token",
                "POST",
                {"term": "别的", "action": "approve"},
            )
        assert broken.value.code == 409
        assert review_path.read_text(encoding="utf-8") == "{not json"

        with pytest.raises(urllib.error.HTTPError) as bad_action:
            _request(
                base + "/api/review/decision",
                "test-token",
                "POST",
                {"term": "别的", "action": "trusted"},
            )
        assert bad_action.value.code == 400
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_the_slang_projection_carries_the_meaning_the_console_edits(
    tmp_path: Path,
) -> None:
    """`SELECT *` is why adding columns was enough — and why renaming one
    would break the browser silently. This pins the browser's field list."""
    from types import SimpleNamespace

    from qunbot.extensions.webui.memory_query import MemoryConsoleQuery
    from qunbot.storage.database import SqliteDatabase
    from qunbot.storage.slang import SlangStore

    database = SqliteDatabase(tmp_path / "bot.sqlite3")
    try:
        store = SlangStore(database)
        store.upsert(
            "group:42", "上大分", occurrences=5, seen_users=["u"],
            seen_days=["d"], samples=[], confidence=0.9, now=1,
        )
        store.set_meaning(
            "group:42", "上大分", "赢了、拿到好处", source="human", stage=0, now=2
        )
        service = SimpleNamespace(
            agent=SimpleNamespace(memories=SimpleNamespace(memories=None)),
            people=None,
            conversations=SimpleNamespace(db=database.db),
        )
        row = MemoryConsoleQuery(service).slang("group:42")[0]
    finally:
        database.close()

    assert row["meaning"] == "赢了、拿到好处"
    assert row["meaning_source"] == "human"
    # The fields the slang cards read.
    for field in ("term", "status", "occurrences", "confidence", "seen_users",
                  "seen_days", "samples"):
        assert field in row


def test_the_persona_queue_is_owner_only_and_records_decisions(tmp_path: Path) -> None:
    """Approving changes no running behaviour — it records what a deployer
    decided, so that adopting a suggestion into the persona file stays their
    own deliberate act."""
    from qunbot.extensions.webui.persona_review import PersonaQueueQuery
    from qunbot.storage.database import SqliteDatabase
    from qunbot.storage.persona_review import ProposalStore

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    database = SqliteDatabase(tmp_path / "bot.sqlite3")
    store = ProposalStore(database)
    store.record("group:42", [{"suggestion": "你应该多说点方言", "rationale": "群里爱用"}], now=1)

    server = WebUIServer(WebUIConfig(True, "127.0.0.1", 0, "test-token", env))
    # `bind_runtime` also builds the memory projection, which needs a fully
    # composed service; this test only exercises the queue.
    server.persona_queue = PersonaQueueQuery(database.db)
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            _request(base + "/api/review/persona")
        assert denied.value.code == 401

        status, body = _request(base + "/api/review/persona", "test-token")
        assert status == 200
        pending = body["data"]["pending"]
        assert len(pending) == 1
        assert pending[0]["suggestion"] == "你应该多说点方言"

        status, body = _request(
            base + f"/api/review/persona/{pending[0]['id']}",
            "test-token",
            "POST",
            {"status": "accepted", "note": "已写进人设"},
        )
        assert status == 200
        assert body["data"]["status"] == "accepted"

        _, after = _request(base + "/api/review/persona", "test-token")
        assert after["data"]["pending"] == []
        assert after["data"]["decided"][0]["note"] == "已写进人设"

        with pytest.raises(urllib.error.HTTPError) as bad:
            _request(
                base + f"/api/review/persona/{pending[0]['id']}",
                "test-token",
                "POST",
                {"status": "maybe"},
            )
        assert bad.value.code == 400

        with pytest.raises(urllib.error.HTTPError) as missing:
            _request(
                base + "/api/review/persona/9999",
                "test-token",
                "POST",
                {"status": "accepted"},
            )
        assert missing.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        database.close()
