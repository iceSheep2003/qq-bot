"""Publication thresholds count changes to published facts, not database size."""

from copy import deepcopy

from qqbot.infrastructure.storage.wiki_sync import LegacyWikiSynchronizer


def snapshot():
    return {"memories": [
        {"id": i, "scope": "group:1", "user_id": "_group_", "content": f"事实{i}",
         "status": "active", "created_at": i, "updated_at": i, "confidence": 0.9}
        for i in range(8)
    ], "topics": [], "slang": []}


def test_one_change_in_large_store_stays_batched(tmp_path):
    data = snapshot()
    now = [100]
    sync = LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: now[0])
    assert sync.refresh().published
    data["memories"][0]["content"] = "更正的事实"
    now[0] = 101
    assert sync.refresh().published is False
    assert "更正的事实" not in (tmp_path / "groups/1.md").read_text()
    now[0] = 700
    assert sync.refresh().published
    assert "更正的事实" in (tmp_path / "groups/1.md").read_text()


def test_only_eight_actual_new_or_changed_records_open_batch_gate(tmp_path):
    data = snapshot()
    sync = LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: 100)
    assert sync.refresh().published
    for index in range(8):
        data["memories"][index]["content"] = f"更正{index}"
        assert sync.refresh().published is (index == 7)


def test_published_delta_baseline_survives_restart(tmp_path):
    data = snapshot()
    assert LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: 100).refresh().published
    data["memories"][0]["content"] = "重启后的更正"
    restarted = LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: 101)
    assert restarted.refresh().published is False


def test_access_counters_do_not_change_published_version_or_rewrite_files(tmp_path):
    data = snapshot()
    sync = LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: 100)
    first = sync.refresh()
    path = tmp_path / "groups/1.md"
    stamp = path.stat().st_mtime_ns
    for row in data["memories"]:
        row["access_count"] = 10
        row["last_accessed_at"] = 101
    second = sync.refresh()
    assert second.changed is False
    assert second.published is False
    assert second.source_version == first.source_version
    assert path.stat().st_mtime_ns == stamp


def test_source_row_order_does_not_change_publication_version(tmp_path):
    data = snapshot()
    sync = LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: 100)
    first = sync.refresh()
    data["memories"].reverse()
    second = sync.refresh()
    assert second.changed is False
    assert second.source_version == first.source_version


def test_deleted_fact_is_removed_on_next_check_without_waiting_for_batch(tmp_path):
    data = snapshot()
    sync = LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: 100)
    assert sync.refresh().published
    data["memories"].pop()
    assert sync.refresh().published
    assert "事实7" not in (tmp_path / "groups/1.md").read_text()


def test_empty_source_can_retire_previous_publication_without_manual_force(tmp_path):
    data = snapshot()
    sync = LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: 100)
    assert sync.refresh().published
    data["memories"].clear()
    assert sync.refresh().published
    assert not (tmp_path / "groups/1.md").exists()


def test_unpublished_candidates_do_not_invalidate_published_context(tmp_path):
    data = snapshot()
    sync = LegacyWikiSynchronizer(lambda: data, tmp_path, clock=lambda: 100)
    first = sync.refresh()
    candidate = deepcopy(data["memories"][0])
    candidate.update(id=99, content="未确认", status="candidate")
    data["memories"].append(candidate)
    second = sync.refresh()
    assert second.changed is False
    assert second.source_version == first.source_version
