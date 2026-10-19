from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from qqbot.interfaces.webui import logbuffer
from qqbot.interfaces.webui.logbuffer import LogRing
from qqbot.interfaces.webui.monitor_query import MonitorQuery

NOW = 1_800_000_000


def trace(scope, created_at, outcome="sent", duration_ms=800):
    return {
        "id": created_at,
        "scope": scope,
        "created_at": created_at,
        "outcome": outcome,
        "duration_ms": duration_ms,
        "steps": [],
    }


class Traces:
    def __init__(self, rows):
        self.rows = rows

    def scan(self, *, scope=None, hours=24, limit=5000):
        return self.rows[:limit]


def gateway(connected=True, accepted=10, dropped=0, failed=0, lanes=2, pending=1):
    return SimpleNamespace(
        connection=object() if connected else None,
        inbound=SimpleNamespace(accepted=accepted, dropped=dropped, completed=accepted, failed=failed),
        pending={"a": object()} if pending else {},
        _dispatcher=SimpleNamespace(lanes=lanes),
    )


def scheduler(running=0, failed_7d=0):
    runs = [{"status": "running"}] * running + [{"status": "failed", "started_at": NOW - 3600}] * failed_7d
    return SimpleNamespace(jobs=SimpleNamespace(list_runs=lambda limit=500: runs))


def monitor(**kwargs):
    defaults = {
        "traces": Traces([trace("groups/1", NOW - 60)]),
        "gateway": gateway(),
        "scheduler": scheduler(),
        "started_at": NOW - 7200,
        "model_configured": True,
        "model_name": "grok",
        "clock": lambda: NOW,
    }
    defaults.update(kwargs)
    return MonitorQuery(**defaults)


def test_healthy_runtime_reports_ok_with_real_counters():
    live = monitor().live()

    assert live["health"]["status"] == "ok"
    assert live["turns"] == 1
    assert live["replies"] == 1
    assert live["uptime_seconds"] == 7200
    assert live["onebot"]["connected"] is True
    assert live["onebot"]["pending"] == 1
    assert live["onebot"]["lanes"] == 2


def test_disconnected_onebot_is_down_and_raises_a_critical_alert():
    query = monitor(gateway=gateway(connected=False))
    live = query.live()

    assert live["health"]["status"] == "down"
    assert "OneBot 未连接" in live["health"]["reasons"][0]
    codes = [item["code"] for item in query.alerts()["items"]]
    assert "onebot_disconnected" in codes


def test_dropped_frames_degrade_health_without_claiming_the_bot_is_down():
    live = monitor(gateway=gateway(dropped=3)).live()

    assert live["health"]["status"] == "degraded"
    assert any("丢弃" in reason for reason in live["health"]["reasons"])


def test_error_ratio_over_threshold_degrades_health():
    rows = [trace("groups/1", NOW - 60, outcome="error") for _ in range(3)]
    rows.append(trace("groups/1", NOW - 30, outcome="sent"))
    live = monitor(traces=Traces(rows)).live()

    assert live["errors"] == 3
    assert live["error_ratio"] == 0.75
    assert live["health"]["status"] == "degraded"


def test_missing_model_configuration_is_down_even_when_the_gateway_is_up():
    live = monitor(model_configured=False).live()

    assert live["health"]["status"] == "down"
    assert any("模型未配置" in reason for reason in live["health"]["reasons"])


def test_sources_report_unknown_instead_of_zero_when_not_bound():
    live = monitor(traces=None, gateway=None, scheduler=None, logs=None).live()

    assert live["sources"] == {"traces": False, "gateway": False, "scheduler": False, "logs": False}
    assert live["onebot"] is None
    assert live["scheduler"] is None
    assert live["turns"] == 0


def test_silence_alert_is_informational_and_scheduler_failures_warn():
    query = monitor(
        traces=Traces([trace("groups/1", NOW - 4000)]),
        scheduler=scheduler(failed_7d=2),
    )
    items = {item["code"]: item for item in query.alerts()["items"]}

    assert items["silent"]["level"] == "info"
    assert items["scheduled_failed"]["level"] == "warning"


def test_series_fills_every_minute_including_quiet_ones():
    rows = [trace("groups/1", NOW - 120), trace("groups/1", NOW - 120, outcome="error")]
    series = monitor(traces=Traces(rows)).series(minutes=5)

    assert series["points"][-1]["at"] == (NOW // 60) * 60
    assert sum(point["turns"] for point in series["points"]) == 2
    assert any(point["turns"] == 0 for point in series["points"])


def test_log_ring_is_bounded_and_newest_first():
    ring = LogRing(capacity=50)
    for index in range(60):
        record = logging.LogRecord("qqbot.test", logging.INFO, __file__, 1, "line %s", (index,), None)
        ring.emit(record)

    items = ring.tail(5)
    assert [item["message"] for item in items] == ["line 59", "line 58", "line 57", "line 56", "line 55"]


def test_tail_reports_unavailable_without_a_ring():
    result = monitor(logs=None).tail()

    assert result["available"] is False
    assert result["items"] == []
    assert result["levels"] == []
    assert result["total"] == 0


@pytest.mark.parametrize("minutes", [0, 999])
def test_live_clamps_the_window_instead_of_failing(minutes):
    live = monitor().live(minutes=minutes)

    assert 1 <= live["window_minutes"] <= 240


def test_a_broken_trace_ledger_does_not_blank_the_board():
    class Broken:
        def scan(self, **_kwargs):
            raise RuntimeError("database is locked")

    live = monitor(traces=Broken()).live()

    assert live["turns"] == 0
    assert live["sources"]["traces"] is True
    assert live["onebot"]["connected"] is True


def test_a_gateway_without_inbound_counters_reports_unknown_rather_than_zero():
    bare = SimpleNamespace(connection=object(), pending={})

    live = monitor(gateway=bare).live()

    assert live["onebot"] is None
    assert live["sources"]["gateway"] is False


def test_a_broken_scheduler_reports_unknown_rather_than_claiming_health():
    class Broken:
        @property
        def jobs(self):
            raise RuntimeError("ledger unavailable")

    live = monitor(scheduler=Broken()).live()

    assert live["scheduler"] is None
    assert live["sources"]["scheduler"] is False


def test_slow_p95_degrades_health_even_when_nothing_failed():
    rows = [trace("groups/1", NOW - 60, duration_ms=25_000)]
    live = monitor(traces=Traces(rows)).live()

    assert live["latency_p95_ms"] == 25_000
    assert live["health"]["status"] == "degraded"
    assert any("P95" in reason for reason in live["health"]["reasons"])


def test_series_ignores_traces_older_than_the_requested_window():
    rows = [trace("groups/1", NOW - 600), trace("groups/1", NOW - 60)]
    series = monitor(traces=Traces(rows)).series(minutes=5)

    assert sum(point["turns"] for point in series["points"]) == 1


def test_alert_list_covers_model_drop_and_error_rate():
    rows = [trace("groups/1", NOW - 60, outcome="error") for _ in range(3)]
    items = {
        item["code"]: item
        for item in monitor(
            traces=Traces(rows),
            gateway=gateway(dropped=4),
            model_configured=False,
        ).alerts()["items"]
    }

    assert items["model_missing"]["level"] == "critical"
    assert items["inbound_dropped"]["level"] == "warning"
    assert "4" in items["inbound_dropped"]["title"]
    assert items["error_rate"]["level"] == "warning"


def test_alert_list_is_sorted_by_severity():
    rows = [trace("groups/1", NOW - 60, outcome="error") for _ in range(3)]
    query = monitor(traces=Traces(rows), gateway=gateway(connected=False), scheduler=scheduler(failed_7d=1))
    levels = [item["level"] for item in query.alerts()["items"]]

    assert levels == sorted(levels, key=["critical", "warning", "info"].index)


def test_tail_serves_the_ring_when_one_is_bound():
    ring = LogRing(capacity=10)
    ring.emit(logging.LogRecord("qqbot.test", logging.INFO, __file__, 1, "boom", None, None))

    tail = monitor(logs=ring).tail(10)

    assert tail["available"] is True
    assert tail["items"][0]["message"] == "boom"


def test_install_is_idempotent_so_handlers_never_stack():
    logger = logging.getLogger("qqbot.test.install")
    logger.handlers.clear()

    first = logbuffer.install("qqbot.test.install")
    stacked = len([item for item in logger.handlers if isinstance(item, LogRing)])
    second = logbuffer.install("qqbot.test.install")

    assert first is second
    assert stacked <= 1
    assert len([item for item in logger.handlers if isinstance(item, LogRing)]) == stacked
    logger.handlers.clear()


def test_a_record_that_cannot_be_formatted_is_dropped_instead_of_raising():
    class Exploding(logging.LogRecord):
        def getMessage(self):
            raise ValueError("bad format")

    ring = LogRing(capacity=10)
    ring.emit(Exploding("qqbot.test", logging.INFO, __file__, 1, "%s", ("x",), None))

    assert ring.tail(10) == []


def test_ring_capacity_has_a_floor_so_a_tiny_request_still_keeps_history():
    ring = LogRing(capacity=1)
    for index in range(60):
        ring.emit(logging.LogRecord("qqbot.test", logging.INFO, __file__, 1, str(index), None, None))

    assert len(ring.tail(200)) == 50


def turn(trace_id, at, scope="groups/1", outcome="sent", duration_ms=800, steps=()):
    return {
        "id": trace_id,
        "scope": scope,
        "created_at": at,
        "outcome": outcome,
        "duration_ms": duration_ms,
        "steps": list(steps),
    }


def test_recent_turns_are_newest_first_with_the_tools_each_one_ran():
    rows = [
        turn(1, NOW - 120, scope="groups/1"),
        turn(
            2,
            NOW - 60,
            scope="groups/2",
            steps=[
                {"stage": "tool", "tool": "search", "outcome": "running"},
                {"stage": "tool", "tool": "search", "outcome": "completed"},
                {"stage": "tool", "tool": "mute", "outcome": "running"},
            ],
        ),
    ]

    result = monitor(traces=Traces(rows)).turns(limit=10)

    assert [row["id"] for row in result["items"]] == [2, 1]
    assert result["items"][0]["scope"] == "groups/2"
    assert result["items"][0]["tools"] == ["search"]
    assert result["items"][0]["steps"] == 3


def test_recent_turns_can_be_narrowed_to_one_conversation():
    rows = [turn(1, NOW - 120, scope="groups/1"), turn(2, NOW - 60, scope="groups/2")]

    result = monitor(traces=Traces(rows)).turns(scope="groups/1")

    assert result["total"] == 1
    assert result["items"][0]["scope"] == "groups/1"


def test_scope_distribution_ranks_the_busiest_conversation_first():
    rows = [
        turn(1, NOW - 60, scope="groups/1"),
        turn(2, NOW - 60, scope="groups/1", outcome="error"),
        turn(3, NOW - 60, scope="groups/2"),
    ]

    result = monitor(traces=Traces(rows)).scopes()

    assert [row["scope"] for row in result["items"]] == ["groups/1", "groups/2"]
    top = result["items"][0]
    assert top["turns"] == 2
    assert top["errors"] == 1
    assert top["error_ratio"] == 0.5
    assert top["p95_ms"] == 800


def test_log_tail_filters_by_level_and_keyword():
    ring = LogRing(capacity=20)
    for level, name, message in [
        (logging.WARNING, "qqbot.gateway", "inbound frame dropped"),
        (logging.ERROR, "qqbot.model", "model timeout"),
        (logging.INFO, "qqbot.agent", "turn completed"),
    ]:
        ring.emit(logging.LogRecord(name, level, __file__, 1, message, None, None))

    query = monitor(logs=ring)
    assert query.tail(level="ERROR")["items"][0]["message"] == "model timeout"
    assert query.tail(q="dropped")["items"][0]["logger"] == "qqbot.gateway"
    assert set(query.tail()["levels"]) == {"INFO", "WARNING", "ERROR"}
    assert query.tail(level="ERROR")["items"][0]["level"] == "ERROR"


def test_recent_turns_are_paged_instead_of_scrolled():
    rows = [turn(index, NOW - index * 60) for index in range(1, 31)]

    first = monitor(traces=Traces(rows)).turns(limit=10)
    second = monitor(traces=Traces(rows)).turns(limit=10, offset=10)

    assert first["total"] == 30
    assert first["pages"] == 3
    assert len(first["items"]) == 10
    assert len(second["items"]) == 10
    assert {row["id"] for row in first["items"]}.isdisjoint({row["id"] for row in second["items"]})
    assert second["items"][0]["id"] == first["items"][-1]["id"] + 1


def test_a_page_past_the_end_is_empty_but_still_reports_the_total():
    rows = [turn(index, NOW - index * 60) for index in range(1, 6)]

    result = monitor(traces=Traces(rows)).turns(limit=10, offset=40)

    assert result["items"] == []
    assert result["total"] == 5
    assert result["pages"] == 1


def test_an_empty_result_still_has_one_page():
    result = monitor(traces=Traces([])).turns(limit=10)

    assert result["pages"] == 1
    assert result["total"] == 0


def test_log_pages_do_not_repeat_entries():
    ring = LogRing(capacity=40)
    for index in range(30):
        ring.emit(logging.LogRecord("qqbot.test", logging.INFO, __file__, 1, f"line {index}", None, None))

    query = monitor(logs=ring)
    first = query.tail(limit=10)
    second = query.tail(limit=10, offset=10)

    assert first["total"] == 30
    assert first["pages"] == 3
    assert [item["message"] for item in first["items"]][0] == "line 29"
    assert {item["message"] for item in first["items"]}.isdisjoint(
        {item["message"] for item in second["items"]}
    )


class TraceSource(Traces):
    """A trace store whose ``detail`` returns a fixed payload when asked."""

    def __init__(self, rows, payload=None):
        super().__init__(rows)
        self.rows_by_id = {row["id"]: row for row in rows}
        self._payload = payload

    def detail(self, trace_id):
        if self._payload is not None:
            return self._payload
        return self.rows_by_id.get(trace_id)


def traced():
    return TraceSource([
        {
            "id": 7,
            "scope": "groups/100200",
            "created_at": NOW - 30,
            "outcome": "sent",
            "duration_ms": 640,
            "event_id": "e7",
            "origin": "message",
            "steps": [
                {"stage": "context", "at_ms": 12, "skills": ["group-chat"], "memory_count": 6},
                {"stage": "model", "at_ms": 120, "model": "deepseek-flash", "prompt_tokens": 2400, "completion_tokens": 180},
                {"stage": "tool", "at_ms": 200, "tool": "group_search", "outcome": "completed"},
                {"stage": "send", "at_ms": 610, "outcome": "sent"},
            ],
        }
    ])


def test_trace_unfolds_the_steps_in_the_order_the_turn_walked_them():
    data = monitor(traces=traced()).trace(7)

    assert data["scope"] == "groups/100200"
    assert data["step_count"] == 4
    assert [step["label"] for step in data["timeline"]] == ["上下文", "模型", "工具", "投递"]
    assert [step["at_ms"] for step in data["timeline"]] == [12, 120, 200, 610]


def test_trace_labels_the_fields_an_operator_reads():
    timeline = monitor(traces=traced()).trace(7)["timeline"]

    context = {field["label"]: field["value"] for field in timeline[0]["fields"]}
    model = {field["label"]: field["value"] for field in timeline[1]["fields"]}

    assert context["命中技能"] == "group-chat"
    assert context["记忆条数"] == 6
    assert model["模型"] == "deepseek-flash"
    assert model["输入 tokens"] == 2400


def test_trace_surfaces_unknown_fields_instead_of_dropping_them():
    source = TraceSource([], payload={
        "id": 9, "scope": "groups/1", "created_at": NOW, "outcome": "sent",
        "steps": [{"stage": "model", "at_ms": 5, "latency_bucket": "fast"}],
    })

    fields = monitor(traces=source).trace(9)["timeline"][0]["fields"]

    assert {"key": "latency_bucket", "label": "latency_bucket", "value": "fast"} in fields


def test_an_unknown_stage_keeps_its_own_name():
    source = TraceSource([], payload={
        "id": 3, "scope": "groups/1", "created_at": NOW, "outcome": "sent",
        "steps": [{"stage": "rerank", "at_ms": 1}],
    })

    assert monitor(traces=source).trace(3)["timeline"][0]["label"] == "rerank"


def test_trace_reports_missing_ids_and_unbound_sources_as_none():
    assert monitor(traces=traced()).trace(404) is None
    assert monitor(traces=traced()).trace("not-a-number") is None
    assert monitor(traces=None).trace(7) is None


def test_trace_keeps_the_raw_payload_for_anyone_chasing_an_odd_value():
    raw = monitor(traces=traced()).trace(7)["raw"]

    assert raw["event_id"] == "e7"
    assert raw["steps"][1]["model"] == "deepseek-flash"


def test_a_tool_start_is_labelled_so_it_does_not_read_as_a_second_call():
    source = TraceSource([], payload={
        "id": 11, "scope": "groups/1", "created_at": NOW, "outcome": "sent",
        "steps": [
            {"stage": "tool", "at_ms": 10, "tool": "search", "outcome": "running"},
            {"stage": "tool", "at_ms": 40, "tool": "search", "outcome": "completed"},
        ],
    })

    labels = [step["label"] for step in monitor(traces=source).trace(11)["timeline"]]

    assert labels == ["工具（开始）", "工具"]


def test_trace_summarises_what_was_injected_into_the_prompt():
    source = TraceSource([], payload={
        "id": 21, "scope": "groups/1", "created_at": NOW, "outcome": "sent",
        "steps": [
            {"stage": "context", "at_ms": 10, "skills": ["group-chat", "meme"], "memory_count": 6,
             "wiki_pages": ["groups-1", "people-42"], "wiki_mode": "topic"},
            {"stage": "route", "at_ms": 20, "route": "hint"},
            {"stage": "model", "at_ms": 30, "tools_offered": 4},
        ],
    })

    injected = monitor(traces=source).trace(21)["injected"]

    assert injected["skills"] == ["group-chat", "meme"]
    assert injected["wiki_pages"] == ["groups-1", "people-42"]
    assert injected["wiki_mode"] == "topic"
    assert injected["memory_count"] == 6
    assert injected["tools_offered"] == 4
    assert injected["route"] == "hint"


def test_injected_summary_is_empty_for_a_turn_without_context_records():
    source = TraceSource([], payload={
        "id": 22, "scope": "groups/1", "created_at": NOW, "outcome": "sent",
        "steps": [{"stage": "delivery", "at_ms": 5, "outcome": "sent"}],
    })

    injected = monitor(traces=source).trace(22)["injected"]

    assert injected["skills"] == []
    assert injected["wiki_pages"] == []
    assert injected["memory_count"] == 0
