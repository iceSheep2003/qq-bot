from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import pytest

from qqbot.infrastructure.storage.database import SqliteDatabase
from qqbot.infrastructure.storage.traces import TraceStore

from qqbot.contracts.telemetry import record, trace_turn


@pytest.fixture
def store(tmp_path: Path):
    database = SqliteDatabase(tmp_path / "trace.db")
    yield TraceStore(database)
    database.close()


def event(event_id: str, scope: str = "group:1"):
    return SimpleNamespace(event_id=event_id, scope=scope, user_id="7", group_id="1", origin="message")


def test_overview_aggregates_skill_and_tool_frequency_by_turn_and_scope(store):
    with trace_turn(event("one"), store):
        record("context", skills=["group-chat", "wiki-recall", "group-chat"])
        record("tool", tool="search", outcome="completed")
        record("tool", tool="calendar", outcome="failed", error_kind="timeout")
    with trace_turn(event("two", "private:7"), store):
        record("context", skills=["private-chat"])
        record("tool", tool="search", outcome="completed")
    overview = store.overview()
    assert overview["skill_counts"] == {"group-chat": 2, "wiki-recall": 1, "private-chat": 1}
    assert overview["tool_counts"] == {"search": 2, "calendar": 1}
    assert overview["tool_outcomes"] == {"completed": 2, "failed": 1}
    assert overview["skill_turns"] == 2
    assert overview["tool_calls"] == 3
    assert overview["skill_scope_counts"]["group:1"]["group-chat"] == 2
    assert overview["tool_scope_counts"]["private:7"]["search"] == 1


def test_frequency_aggregation_preserves_unknown_and_empty_values(store):
    with trace_turn(event("one"), store):
        record("context", skills=[])
        record("tool", tool="", outcome="unknown")
    overview = store.overview()
    assert overview["skill_counts"] == {}
    assert overview["tool_counts"] == {}
    assert overview["tool_outcomes"] == {"unknown": 1}
    assert overview["tool_calls"] == 1


def test_frequency_aggregation_respects_scope_filter(store):
    with trace_turn(event("one"), store):
        record("context", skills=["group-chat"])
        record("tool", tool="search", outcome="completed")
    with trace_turn(event("two", "private:7"), store):
        record("context", skills=["private-chat"])
        record("tool", tool="calendar", outcome="completed")
    overview = store.overview(scope="private:7")
    assert overview["skill_counts"] == {"private-chat": 1}
    assert overview["tool_counts"] == {"calendar": 1}
    assert "group-chat" not in overview["skill_counts"]
