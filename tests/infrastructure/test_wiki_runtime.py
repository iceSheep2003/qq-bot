from __future__ import annotations

from qqbot.infrastructure.storage.wiki_runtime import configured_wiki_reader


def test_wiki_runtime_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_WIKI_ENABLED", raising=False)
    monkeypatch.setenv("BOT_WIKI_ROOT", str(tmp_path))

    assert configured_wiki_reader() is None


def test_enabled_wiki_runtime_reads_published_markdown(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_WIKI_ENABLED", "true")
    monkeypatch.setenv("BOT_WIKI_ROOT", str(tmp_path))

    reader = configured_wiki_reader()

    assert reader is not None
    assert reader.read("group:missing", "u1", "测试")["pages"] == []
