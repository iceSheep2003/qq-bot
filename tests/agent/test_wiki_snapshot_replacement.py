"""Publishing a replacement removes retired generated pages, never handwritten notes."""

import json

import pytest

from qqbot.agent.memory.wiki.migration import WikiMigration
from qqbot.agent.memory.wiki.writer import WikiWriter
from qqbot.infrastructure.storage.wiki_runtime import FileBackedWikiReader
from qqbot.infrastructure.storage.wiki_sync import LegacyWikiSynchronizer


def rows():
    return {"memories": [
        {"id": 1, "scope": "group:1", "user_id": "_group_", "content": "群的事实", "status": "active"},
        {"id": 2, "scope": "group:1", "user_id": "7", "content": "个人事实", "status": "active"},
    ], "topics": [], "slang": []}


def test_retired_generated_page_is_removed_across_migration_instances(tmp_path):
    snapshot = rows()
    WikiMigration().run(snapshot, tmp_path, write=True)
    snapshot["memories"].pop()
    WikiMigration().run(snapshot, tmp_path, write=True)
    assert (tmp_path / "groups/1.md").is_file()
    assert not (tmp_path / "groups/1/members/7.md").exists()


def test_empty_replacement_removes_only_owned_files(tmp_path):
    WikiMigration().run(rows(), tmp_path, write=True)
    handwritten = tmp_path / "notes.md"
    handwritten.write_text("# 手写说明\n保留原文")
    WikiMigration().run({}, tmp_path, write=True)
    assert handwritten.read_text() == "# 手写说明\n保留原文"
    assert not (tmp_path / "groups/1.md").exists()
    assert not (tmp_path / "groups/1/members/7.md").exists()


def test_dry_run_does_not_change_owned_page_manifest_or_content(tmp_path):
    WikiMigration().run(rows(), tmp_path, write=True)
    manifest = tmp_path / ".wiki-managed-pages.json"
    assert manifest.is_file()
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    WikiMigration().run({}, tmp_path, write=False)
    after = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("payload", [
    {"version": 1, "pages": ["../outside"]},
    {"version": 1, "pages": "groups/1"},
    {"version": 2, "pages": []},
])
def test_invalid_ownership_manifest_cannot_trigger_deletion(tmp_path, payload):
    root = tmp_path / "wiki"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("保留")
    (root / ".wiki-managed-pages.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        WikiMigration().run({}, root, write=True)
    assert outside.read_text() == "保留"


def test_forced_sync_removes_retired_page_and_invalidates_warm_reader_cache(tmp_path):
    snapshot = rows()
    sync = LegacyWikiSynchronizer(lambda: snapshot, tmp_path)
    sync.refresh(force=True)
    reader = FileBackedWikiReader(tmp_path, ttl_seconds=300)
    assert reader.read("group:1", "7", "事实")["entries"]
    snapshot["memories"].clear()
    sync.refresh(force=True)
    assert reader.read("group:1", "7", "事实")["entries"] == []


def test_failed_publication_does_not_replace_ownership_or_remove_old_pages(tmp_path):
    WikiMigration().run(rows(), tmp_path, write=True)
    manifest = tmp_path / ".wiki-managed-pages.json"
    assert manifest.is_file()
    before = manifest.read_bytes()
    class BrokenWriter(WikiWriter):
        def write(self, root, page):
            raise OSError("write failed")
    snapshot = rows()
    snapshot["memories"] = [{**snapshot["memories"][0], "scope": "group:2"}]
    with pytest.raises(OSError):
        WikiMigration(writer=BrokenWriter()).run(snapshot, tmp_path, write=True)
    assert manifest.read_bytes() == before
    assert (tmp_path / "groups/1.md").is_file()
    assert (tmp_path / "groups/1/members/7.md").is_file()
