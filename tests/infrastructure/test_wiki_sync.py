from __future__ import annotations

from qqbot.agent.memory.wiki.update_policy import WikiUpdatePolicy
from qqbot.infrastructure.storage.wiki_sync import LegacyWikiSynchronizer


def test_sync_batches_changed_legacy_rows_before_writing(tmp_path):
    now = [100]
    snapshot = {
        "memories": [{"id": 1, "scope": "group:1", "user_id": "_group_", "content": "旧事实", "status": "active", "created_at": 1, "updated_at": 1, "confidence": 0.8}],
        "topics": [],
        "slang": [],
    }
    sync = LegacyWikiSynchronizer(
        lambda: snapshot,
        tmp_path,
        policy=WikiUpdatePolicy(min_interval_seconds=600, max_pending_entries=8),
        clock=lambda: now[0],
    )

    first = sync.refresh()
    assert first.changed is True
    assert first.published is False
    assert not (tmp_path / "groups/1.md").exists()

    snapshot["memories"].extend(
        {"id": i, "scope": "group:1", "user_id": "_group_", "content": f"事实{i}", "status": "active", "created_at": i, "updated_at": i, "confidence": 0.8}
        for i in range(2, 9)
    )
    second = sync.refresh()
    assert second.published is True
    assert (tmp_path / "groups/1.md").exists()


def test_sync_can_be_forced_for_manual_correction(tmp_path):
    snapshot = {"memories": [{"id": 1, "scope": "group:1", "user_id": "_group_", "content": "事实", "status": "active", "created_at": 1, "updated_at": 1, "confidence": 0.8}], "topics": [], "slang": []}
    sync = LegacyWikiSynchronizer(lambda: snapshot, tmp_path, clock=lambda: 100)

    report = sync.refresh(force=True)

    assert report.published is True
