"""Read-only tool-call projection over persisted turn traces.

Each tool invocation writes two trace steps: ``running`` before the call and a
terminal outcome after it. Counting steps would therefore double every call, so
this projection counts terminal steps and uses a trailing ``running`` step only
to report work that never finished. Tool steps carry no duration of their own;
the console shows ``None`` rather than borrowing the turn's latency.
"""

from __future__ import annotations

TERMINAL = frozenset({"completed", "error", "failed"})
MAX_TRACES = 5000


class ToolConsoleQuery:
    def __init__(self, traces):
        self.traces = traces

    def _scan(self, scope, hours):
        if self.traces is None:
            return []
        return self.traces.scan(scope=scope, hours=hours, limit=MAX_TRACES)

    @staticmethod
    def _bucket(outcome):
        if outcome == "completed":
            return "completed"
        if outcome in {"error", "failed"}:
            return "failed"
        return "unknown"

    def overview(self, scope=None, hours=24):
        if not 1 <= int(hours) <= 168:
            raise ValueError("hours must be between 1 and 168")
        traces = self._scan(scope, hours)
        tools: dict[str, dict] = {}
        outcomes: dict[str, int] = {}
        timeline: dict[int, dict] = {}
        total = 0
        inflight = 0
        for trace in traces:
            created = trace.get("created_at") or 0
            trace_scope = str(trace.get("scope") or "")
            started: dict[str, int] = {}
            finished: dict[str, int] = {}
            for step in trace.get("steps", ()):
                if step.get("stage") != "tool":
                    continue
                name = str(step.get("tool") or "未命名")
                outcome = str(step.get("outcome") or "unknown")
                if outcome == "running":
                    started[name] = started.get(name, 0) + 1
                    continue
                finished[name] = finished.get(name, 0) + 1
                total += 1
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                row = tools.setdefault(
                    name,
                    {
                        "tool": name,
                        "calls": 0,
                        "completed": 0,
                        "failed": 0,
                        "unknown": 0,
                        "last_at": 0,
                        "scopes": {},
                    },
                )
                row["calls"] += 1
                row[self._bucket(outcome)] += 1
                row["last_at"] = max(row["last_at"], int(created))
                row["scopes"][trace_scope] = row["scopes"].get(trace_scope, 0) + 1
                minute = int(created // 60) * 60
                bucket = timeline.setdefault(minute, {"at": minute, "calls": 0, "failed": 0})
                bucket["calls"] += 1
                bucket["failed"] += self._bucket(outcome) == "failed"
            for name, count in started.items():
                inflight += max(0, count - finished.get(name, 0))
        rows = sorted(tools.values(), key=lambda row: (-row["calls"], row["tool"]))
        for row in rows:
            row["success_rate"] = (
                round(row["completed"] / row["calls"], 4) if row["calls"] else None
            )
            row["scope_count"] = len(row["scopes"])
            row["scopes"] = dict(sorted(row["scopes"].items(), key=lambda item: -item[1]))
        return {
            "available": self.traces is not None,
            "hours": int(hours),
            "scope": scope,
            "total_calls": total,
            "traces_scanned": len(traces),
            "truncated": len(traces) >= MAX_TRACES,
            "inflight": inflight,
            "outcomes": dict(sorted(outcomes.items(), key=lambda item: -item[1])),
            "tools": rows,
            "timeline": sorted(timeline.values(), key=lambda bucket: bucket["at"]),
        }

    def calls(self, scope=None, hours=24, tool=None, limit=100, offset=0):
        if not 1 <= int(hours) <= 168:
            raise ValueError("hours must be between 1 and 168")
        if not 1 <= int(limit) <= 200:
            raise ValueError("limit must be between 1 and 200")
        limit = int(limit)
        offset = max(0, int(offset))
        traces = self._scan(scope, hours)
        rows = []
        for trace in traces:
            for step in trace.get("steps", ()):
                if step.get("stage") != "tool":
                    continue
                outcome = str(step.get("outcome") or "unknown")
                if outcome == "running":
                    continue
                name = str(step.get("tool") or "未命名")
                if tool and name != tool:
                    continue
                rows.append(
                    {
                        "trace_id": trace.get("id"),
                        "at": trace.get("created_at"),
                        "scope": trace.get("scope"),
                        "tool": name,
                        "outcome": outcome,
                        "at_ms": step.get("at_ms"),
                        "error_kind": step.get("error_kind"),
                        "duration_ms": step.get("duration_ms"),
                    }
                )
        rows.sort(key=lambda row: (row["at"] or 0, row["trace_id"] or 0), reverse=True)
        total = len(rows)
        return {
            "items": rows[offset:offset + limit],
            "total": total,
            "truncated": total > offset + limit,
            "hours": int(hours),
            "scope": scope,
            "tool": tool,
            "offset": offset,
            "limit": limit,
            "pages": max(1, -(-total // limit)),
        }
