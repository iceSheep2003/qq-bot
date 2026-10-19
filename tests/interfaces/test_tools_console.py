from __future__ import annotations

import pytest

from qqbot.interfaces.webui.tools_query import ToolConsoleQuery


def trace(trace_id, scope, created_at, steps):
    return {"id": trace_id, "scope": scope, "created_at": created_at, "steps": steps}


def tool_step(name, outcome):
    return {"stage": "tool", "tool": name, "outcome": outcome, "at_ms": 1}


class Traces:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def scan(self, *, scope=None, hours=24, limit=5000):
        self.calls.append((scope, hours, limit))
        return [row for row in self.rows if scope is None or row["scope"] == scope][:limit]


def test_running_and_terminal_steps_count_as_one_call():
    rows = [
        trace(2, "groups/1", 200, [tool_step("search", "running"), tool_step("search", "completed")]),
        trace(1, "groups/1", 100, [tool_step("mute", "running"), tool_step("mute", "error")]),
    ]
    result = ToolConsoleQuery(Traces(rows)).overview()

    assert result["total_calls"] == 2
    assert result["outcomes"] == {"completed": 1, "error": 1}
    assert [row["tool"] for row in result["tools"]] == ["mute", "search"]
    by_name = {row["tool"]: row for row in result["tools"]}
    assert by_name["search"]["completed"] == 1
    assert by_name["mute"]["failed"] == 1
    assert by_name["mute"]["success_rate"] == 0.0
    assert by_name["search"]["success_rate"] == 1.0


def test_trailing_running_step_is_reported_as_inflight_not_as_a_call():
    rows = [trace(1, "groups/1", 100, [tool_step("slow-tool", "running")])]
    result = ToolConsoleQuery(Traces(rows)).overview()

    assert result["total_calls"] == 0
    assert result["inflight"] == 1
    assert result["tools"] == []


def test_an_unfinished_tool_is_counted_even_when_other_tools_followed_it():
    rows = [
        trace(
            1,
            "groups/1",
            100,
            [tool_step("voice_send", "running"), tool_step("search", "running"), tool_step("search", "completed")],
        )
    ]
    result = ToolConsoleQuery(Traces(rows)).overview()

    assert result["total_calls"] == 1
    assert result["inflight"] == 1
    assert [row["tool"] for row in result["tools"]] == ["search"]


def test_unknown_outcome_is_kept_visible_instead_of_counted_as_success():
    rows = [trace(1, "groups/1", 100, [tool_step("odd", "timeout")])]
    result = ToolConsoleQuery(Traces(rows)).overview()

    assert result["outcomes"] == {"timeout": 1}
    row = result["tools"][0]
    assert row["unknown"] == 1
    assert row["completed"] == 0
    assert row["success_rate"] == 0.0


def test_scope_and_tool_filters_apply_to_the_call_list():
    rows = [
        trace(3, "groups/1", 300, [tool_step("search", "completed")]),
        trace(2, "groups/2", 200, [tool_step("search", "completed")]),
        trace(1, "groups/1", 100, [tool_step("mute", "completed")]),
    ]
    query = ToolConsoleQuery(Traces(rows))

    scoped = query.calls(scope="groups/1")
    assert scoped["total"] == 2
    assert {row["scope"] for row in scoped["items"]} == {"groups/1"}

    filtered = query.calls(tool="mute")
    assert filtered["total"] == 1
    assert filtered["items"][0]["tool"] == "mute"
    assert filtered["items"][0]["trace_id"] == 1


def test_call_rows_never_invent_a_duration():
    rows = [trace(1, "groups/1", 100, [tool_step("search", "completed")])]
    item = ToolConsoleQuery(Traces(rows)).calls()["items"][0]

    assert item["duration_ms"] is None
    assert item["outcome"] == "completed"


def test_projection_is_explicit_when_no_trace_source_is_bound():
    result = ToolConsoleQuery(None).overview()

    assert result["available"] is False
    assert result["total_calls"] == 0
    assert result["tools"] == []


@pytest.mark.parametrize("hours", [0, 169])
def test_overview_rejects_out_of_range_hours(hours):
    with pytest.raises(ValueError):
        ToolConsoleQuery(Traces([])).overview(hours=hours)


def test_calls_reject_invalid_limit():
    with pytest.raises(ValueError):
        ToolConsoleQuery(Traces([])).calls(limit=0)


def test_timeline_buckets_calls_per_minute():
    rows = [
        trace(2, "groups/1", 130, [tool_step("search", "completed")]),
        trace(1, "groups/1", 100, [tool_step("search", "error")]),
    ]
    timeline = ToolConsoleQuery(Traces(rows)).overview()["timeline"]

    assert timeline[0]["at"] == 60
    assert timeline[0]["calls"] == 1
    assert timeline[0]["failed"] == 1
    assert timeline[1]["calls"] == 1
    assert timeline[1]["failed"] == 0


def test_overview_marks_truncation_when_the_scan_bound_is_hit():
    rows = [trace(index, "groups/1", 100 + index, [tool_step("t", "completed")]) for index in range(3)]
    query = ToolConsoleQuery(Traces(rows))
    result = query.overview()

    assert result["traces_scanned"] == 3
    assert result["truncated"] is False


def test_scope_counts_are_sorted_by_frequency():
    rows = [
        trace(3, "groups/1", 300, [tool_step("search", "completed")]),
        trace(2, "groups/1", 200, [tool_step("search", "completed")]),
        trace(1, "groups/2", 100, [tool_step("search", "completed")]),
    ]
    row = ToolConsoleQuery(Traces(rows)).overview()["tools"][0]

    assert list(row["scopes"].items())[0] == ("groups/1", 2)
    assert row["scope_count"] == 2


def test_unnamed_tool_stays_visible():
    rows = [trace(1, "groups/1", 100, [{"stage": "tool", "outcome": "completed"}])]
    result = ToolConsoleQuery(Traces(rows)).overview()

    assert result["tools"][0]["tool"] == "未命名"


def test_non_tool_steps_are_ignored_by_both_projections():
    steps = [
        {"stage": "model", "outcome": "completed"},
        {"stage": "tool", "tool": "search", "outcome": "completed"},
        {"stage": "delivery", "outcome": "completed"},
    ]
    rows = [trace(1, "groups/1", 100, steps)]
    query = ToolConsoleQuery(Traces(rows))

    assert query.overview()["total_calls"] == 1
    assert [item["tool"] for item in query.calls()["items"]] == ["search"]


def test_call_list_omits_running_steps():
    rows = [
        trace(
            1,
            "groups/1",
            100,
            [tool_step("search", "running"), tool_step("search", "completed"), tool_step("slow", "running")],
        )
    ]
    result = ToolConsoleQuery(Traces(rows)).calls()

    assert result["total"] == 1
    assert result["items"][0]["outcome"] == "completed"


@pytest.mark.parametrize("hours", [0, 169])
def test_calls_reject_out_of_range_hours(hours):
    with pytest.raises(ValueError, match="hours must be between 1 and 168"):
        ToolConsoleQuery(Traces([])).calls(hours=hours)


def test_call_list_is_paged_and_reports_the_full_total():
    rows = [trace(index, "groups/1", index * 10, [tool_step("search", "completed")]) for index in range(1, 26)]
    query = ToolConsoleQuery(Traces(rows))

    first = query.calls(limit=10)
    last = query.calls(limit=10, offset=20)

    assert first["total"] == 25
    assert first["pages"] == 3
    assert len(first["items"]) == 10
    assert len(last["items"]) == 5
    assert last["truncated"] is False
    assert first["truncated"] is True
