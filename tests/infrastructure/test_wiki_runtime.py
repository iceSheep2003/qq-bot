from __future__ import annotations

from qqbot.infrastructure.storage.wiki_runtime import FileBackedWikiReader, configured_wiki_reader
from qqbot.agent.memory.wiki.page import WikiEntry, WikiPage
from qqbot.agent.memory.wiki.writer import WikiWriter


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


def test_enabled_runtime_refreshes_after_markdown_snapshot_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_WIKI_ENABLED", "true")
    monkeypatch.setenv("BOT_WIKI_ROOT", str(tmp_path))
    reader = configured_wiki_reader()
    assert reader is not None

    WikiWriter().write(
        tmp_path,
        WikiPage(
            page_id="groups/1",
            scope="group",
            entity_id="_group_",
            title="群",
            entries=(WikiEntry("e1", "第一条", observed_at=1),),
            version="v1",
        ),
    )

    result = reader.read("group:1", "u1", "第一")

    assert result["entries"][0]["claim"] == "第一条"


def test_file_backed_reader_runs_optional_synchronizer_before_read(tmp_path):
    calls = []

    class Synchronizer:
        def refresh(self):
            calls.append(True)

    reader = FileBackedWikiReader(tmp_path, ttl_seconds=300, synchronizer=Synchronizer())

    reader.read("group:1", "u1", "测试")

    assert calls == [True]
