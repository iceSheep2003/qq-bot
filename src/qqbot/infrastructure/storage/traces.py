"""Durable, bounded operational trace projection for the owner console."""
from __future__ import annotations

import json
import math
import time

from qqbot.infrastructure.storage.base import SqliteRepository


def migrate(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS turn_traces (
          id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,
          created_at REAL NOT NULL,payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS trace_scope_time ON turn_traces(scope,created_at);
        CREATE INDEX IF NOT EXISTS trace_time ON turn_traces(created_at);
    """)


class TraceStore(SqliteRepository):
    """At most 50k turns / 30 days; bounded payloads contain no transcripts."""

    def save(self, trace):
        with self.transaction():
            self.db.execute("INSERT INTO turn_traces(scope,created_at,payload) VALUES(?,?,?)",
                            (trace["scope"], trace["created_at"], json.dumps(trace, ensure_ascii=False)))
            self.db.execute("DELETE FROM turn_traces WHERE created_at < ? OR id <= (SELECT COALESCE(MAX(id),0)-50000 FROM turn_traces)", (time.time()-30*86400,))

    @staticmethod
    def _filter(scope, hours):
        if not 1 <= hours <= 168:
            raise ValueError("hours must be between 1 and 168")
        where, params = "created_at>=?", [time.time() - hours*3600]
        if scope:
            where += " AND scope=?"
            params.append(scope)
        return where, params

    @staticmethod
    def _row(row):
        return {**json.loads(row["payload"]), "id": row["id"]}

    def list(self, *, scope=None, hours=24, limit=50, before=None):
        if not 1 <= limit <= 200 or (before is not None and before < 1):
            raise ValueError("invalid limit or cursor")
        where, params = self._filter(scope, hours)
        if before is not None:
            where += " AND id<?"
            params.append(before)
        with self.database._transaction_lock:
            rows = self.db.execute(f"SELECT * FROM turn_traces WHERE {where} ORDER BY id DESC LIMIT ?", (*params, limit+1)).fetchall()
        items = [self._row(row) for row in rows[:limit]]
        return {"items": items, "next_cursor": items[-1]["id"] if len(rows)>limit else None}

    def detail(self, trace_id):
        with self.database._transaction_lock:
            row = self.db.execute("SELECT * FROM turn_traces WHERE id=?", (trace_id,)).fetchone()
        return self._row(row) if row else None

    def scan(self, *, scope=None, hours=24, limit=5000):
        """Newest-first traces for a projection that aggregates every step.

        ``list`` is paginated for the trace viewer; a projection needs a bounded
        window it can walk once, so the bound is explicit and reported by the
        caller instead of silently dropping rows.
        """
        if not 1 <= limit <= 50000:
            raise ValueError("limit must be between 1 and 50000")
        where, params = self._filter(scope, hours)
        with self.database._transaction_lock:
            rows = self.db.execute(
                f"SELECT * FROM turn_traces WHERE {where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._row(row) for row in rows]

    def overview(self, *, scope=None, hours=24):
        where, params = self._filter(scope, hours)
        with self.database._transaction_lock:
            rows = self.db.execute(f"SELECT payload FROM turn_traces WHERE {where}", params).fetchall()
        traces = [json.loads(row[0]) for row in rows]
        result = {"turns": len(traces), "replies": 0, "errors": 0, "model_calls": 0,
                  "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                  "cache_known_calls": 0, "cache_unknown_calls": 0, "token_unknown_calls": 0,
                  "cache_hit_ratio": None, "latency_p95_ms": None, "hours": hours,
                  "retention_days": 30, "retention_turns": 50000,
                  "skill_counts": {}, "tool_counts": {}, "tool_outcomes": {},
                  "skill_turns": 0, "tool_calls": 0, "skill_scope_counts": {},
                  "tool_scope_counts": {}}
        denominator = 0
        buckets, scopes, outcomes, models = {}, set(), {}, {}
        skill_scope_counts, tool_scope_counts = {}, {}
        for trace in traces:
            scopes.add(trace["scope"])
            skill_scope_counts.setdefault(trace["scope"], {})
            tool_scope_counts.setdefault(trace["scope"], {})
            outcome = trace["outcome"]
            outcomes[outcome] = outcomes.get(outcome, 0)+1
            result["replies"] += outcome == "sent"
            result["errors"] += outcome == "error"
            hour = int(trace["created_at"]//3600)*3600
            bucket = buckets.setdefault(hour, {"at": hour, "turns": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})
            bucket["turns"] += 1
            for step in trace["steps"]:
                if step["stage"] == "context":
                    skills = step.get("skills") or []
                    if skills:
                        result["skill_turns"] += 1
                    for name in skills:
                        if not name:
                            continue
                        result["skill_counts"][name] = result["skill_counts"].get(name, 0) + 1
                        skill_scope_counts[trace["scope"]][name] = skill_scope_counts[trace["scope"]].get(name, 0) + 1
                    continue
                if step["stage"] == "tool":
                    result["tool_calls"] += 1
                    name = step.get("tool")
                    if name:
                        result["tool_counts"][name] = result["tool_counts"].get(name, 0) + 1
                        tool_scope_counts[trace["scope"]][name] = tool_scope_counts[trace["scope"]].get(name, 0) + 1
                    tool_outcome = step.get("outcome", "unknown") or "unknown"
                    result["tool_outcomes"][tool_outcome] = result["tool_outcomes"].get(tool_outcome, 0) + 1
                    continue
                if step["stage"] != "model":
                    continue
                result["model_calls"] += 1
                name = step.get("model", "未报告")
                models[name] = models.get(name, 0)+1
                for field in ("prompt_tokens", "completion_tokens"):
                    value = step.get(field)
                    if isinstance(value, int) and value >= 0:
                        result[field] += value
                        bucket[field] += value
                prompt, cached = step.get("prompt_tokens"), step.get("cached_tokens")
                if not isinstance(prompt, int) or not isinstance(step.get("completion_tokens"), int):
                    result["token_unknown_calls"] += 1
                if isinstance(prompt, int) and isinstance(cached, int) and 0 <= cached <= prompt:
                    result["cache_known_calls"] += 1
                    result["cached_tokens"] += cached
                    bucket["cached_tokens"] += cached
                    denominator += prompt
                else:
                    result["cache_unknown_calls"] += 1
        if denominator:
            result["cache_hit_ratio"] = result["cached_tokens"]/denominator
        durations = sorted(t["duration_ms"] for t in traces)
        if durations:
            result["latency_p95_ms"] = durations[max(0, math.ceil(len(durations)*.95)-1)]
        return {**result, "timeline": sorted(buckets.values(), key=lambda x:x["at"]),
                "scopes": sorted(scopes), "outcomes": outcomes, "models": models,
                "skill_scope_counts": skill_scope_counts, "tool_scope_counts": tool_scope_counts}
