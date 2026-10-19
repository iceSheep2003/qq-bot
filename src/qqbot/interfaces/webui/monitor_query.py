"""Live operations projection for the owner console.

Health is derived from the sources the running process already owns: the
OneBot gateway's connection and inbound counters, the scheduler's run ledger and
the persisted turn traces. A missing source reports ``None`` (unknown) instead
of a comforting zero, because "no data" and "nothing wrong" are different
answers during an incident.
"""

from __future__ import annotations

import time

# Alerts are ordered by severity so the console can sort without guessing.
LEVELS = ("critical", "warning", "info")


def _page(rows, limit, offset, **extra):
    """Slice a list into one page and describe the whole set.

    ``pages`` is always at least 1 so the console never has to guard against a
    zero-page state on an empty table.
    """
    total = len(rows)
    return {
        "items": rows[offset:offset + limit],
        "total": total,
        "offset": offset,
        "limit": limit,
        "pages": max(1, -(-total // limit)),
        **extra,
    }


class MonitorQuery:
    def __init__(
        self,
        *,
        traces=None,
        gateway=None,
        scheduler=None,
        logs=None,
        started_at=None,
        model_configured=True,
        model_name="",
        clock=time.time,
    ):
        self.traces = traces
        self.gateway = gateway
        self.scheduler = scheduler
        self.logs = logs
        self.started_at = started_at
        self.model_configured = bool(model_configured)
        self.model_name = model_name or ""
        self.clock = clock

    # -- sources ---------------------------------------------------------

    def _scan(self, hours, limit=2000):
        if self.traces is None:
            return []
        try:
            return self.traces.scan(scope=None, hours=hours, limit=limit)
        except Exception:  # a broken ledger must not blank the whole board
            return []

    def _inbound(self):
        gateway = self.gateway
        if gateway is None:
            return None
        stats = getattr(gateway, "inbound", None)
        if stats is None:
            return None
        dispatcher = getattr(gateway, "_dispatcher", None)
        return {
            "connected": getattr(gateway, "connection", None) is not None,
            "accepted": int(getattr(stats, "accepted", 0) or 0),
            "dropped": int(getattr(stats, "dropped", 0) or 0),
            "completed": int(getattr(stats, "completed", 0) or 0),
            "failed": int(getattr(stats, "failed", 0) or 0),
            "lanes": getattr(dispatcher, "lanes", None),
            "pending": len(getattr(gateway, "pending", {}) or {}),
        }

    def _scheduled(self):
        if self.scheduler is None:
            return None
        try:
            runs = self.scheduler.jobs.list_runs(limit=500)
        except Exception:
            return None
        now = self.clock()
        return {
            "running": sum(run.get("status") == "running" for run in runs),
            "failed_7d": sum(
                run.get("status") == "failed"
                and (run.get("started_at") or 0) >= now - 604800
                for run in runs
            ),
            "runs_sampled": len(runs),
        }

    # -- projections -----------------------------------------------------

    def live(self, minutes=15):
        minutes = max(1, min(240, int(minutes)))
        now = self.clock()
        traces = self._scan(24)
        last_at = max((trace.get("created_at") or 0 for trace in traces), default=0)
        window = now - minutes * 60
        recent = [trace for trace in traces if (trace.get("created_at") or 0) >= window]
        replies = sum(1 for trace in recent if trace.get("outcome") == "sent")
        errors = sum(1 for trace in recent if trace.get("outcome") == "error")
        durations = sorted(
            trace.get("duration_ms") for trace in recent if isinstance(trace.get("duration_ms"), (int, float))
        )
        p95 = durations[max(0, -(-len(durations) * 95 // 100) - 1)] if durations else None
        inbound = self._inbound()
        scheduled = self._scheduled()
        health, reasons = self._verdict(inbound, recent, errors, p95)
        return {
            "now": now,
            "uptime_seconds": int(now - self.started_at) if self.started_at else None,
            "window_minutes": minutes,
            "turns": len(recent),
            "replies": replies,
            "errors": errors,
            "error_ratio": round(errors / len(recent), 4) if recent else None,
            "latency_p95_ms": p95,
            "last_turn_at": last_at or None,
            "silent_seconds": int(now - last_at) if last_at else None,
            "model": {"name": self.model_name, "configured": self.model_configured},
            "onebot": inbound,
            "scheduler": scheduled,
            "health": {"status": health, "reasons": reasons},
            "sources": {
                "traces": self.traces is not None,
                "gateway": inbound is not None,
                "scheduler": scheduled is not None,
                "logs": self.logs is not None,
            },
        }

    def _verdict(self, inbound, recent, errors, p95):
        reasons = []
        status = "ok"
        if not self.model_configured:
            status = "down"
            reasons.append("模型未配置：缺少模型名称或 API 地址")
        if inbound is not None and not inbound["connected"]:
            status = "down"
            reasons.append("OneBot 未连接：NapCat 掉线后收不到任何消息")
        if inbound is not None and inbound["dropped"]:
            status = "degraded" if status == "ok" else status
            reasons.append(f"入站丢弃 {inbound['dropped']} 帧：队列或并发通道已满")
        if recent:
            ratio = errors / len(recent)
            if ratio >= 0.3:
                status = "degraded" if status == "ok" else status
                reasons.append(f"最近 {len(recent)} 轮里 {errors} 轮失败（{ratio:.0%}）")
        if p95 is not None and p95 >= 20000:
            status = "degraded" if status == "ok" else status
            reasons.append(f"P95 延迟 {int(p95)} ms，超过 20 秒")
        if status == "ok" and not reasons:
            reasons.append("所有已接入信号正常")
        return status, reasons

    def series(self, minutes=60):
        minutes = max(5, min(360, int(minutes)))
        traces = self._scan(6, limit=5000)
        now = self.clock()
        floor = int((now - minutes * 60) // 60) * 60
        buckets: dict[int, dict] = {}
        for trace in traces:
            created = trace.get("created_at") or 0
            if created < floor:
                continue
            minute = int(created // 60) * 60
            bucket = buckets.setdefault(minute, {"at": minute, "turns": 0, "replies": 0, "errors": 0})
            bucket["turns"] += 1
            bucket["replies"] += trace.get("outcome") == "sent"
            bucket["errors"] += trace.get("outcome") == "error"
        filled = []
        cursor = floor
        while cursor <= int(now // 60) * 60:
            filled.append(buckets.get(cursor, {"at": cursor, "turns": 0, "replies": 0, "errors": 0}))
            cursor += 60
        return {"minutes": minutes, "points": filled}

    def alerts(self, minutes=15):
        live = self.live(minutes)
        inbound = live["onebot"]
        alerts = []
        if not live["model"]["configured"]:
            alerts.append(
                {
                    "level": "critical",
                    "code": "model_missing",
                    "title": "模型未配置",
                    "detail": "主对话模型名称为空，Bot 无法生成回复。",
                }
            )
        if inbound is not None and not inbound["connected"]:
            alerts.append(
                {
                    "level": "critical",
                    "code": "onebot_disconnected",
                    "title": "OneBot 未连接",
                    "detail": "NapCat 的 WebSocket 没有连上，入站消息全部丢失。",
                }
            )
        if inbound is not None and inbound["dropped"]:
            alerts.append(
                {
                    "level": "warning",
                    "code": "inbound_dropped",
                    "title": f"入站丢弃 {inbound['dropped']} 帧",
                    "detail": "消息在进入处理前被拒绝，检查 BOT_ONEBOT_MAX_LANES 与队列容量。",
                }
            )
        if live["error_ratio"] is not None and live["error_ratio"] >= 0.3:
            alerts.append(
                {
                    "level": "warning",
                    "code": "error_rate",
                    "title": f"失败率 {live['error_ratio']:.0%}",
                    "detail": f"最近 {live['window_minutes']} 分钟 {live['errors']}/{live['turns']} 轮失败。",
                }
            )
        if live["silent_seconds"] is not None and live["silent_seconds"] >= 1800:
            alerts.append(
                {
                    "level": "info",
                    "code": "silent",
                    "title": "超过 30 分钟没有消息",
                    "detail": "可能是群里确实没人说话，也可能是掉线；结合 OneBot 状态判断。",
                }
            )
        scheduled = live["scheduler"]
        if scheduled and scheduled["failed_7d"]:
            alerts.append(
                {
                    "level": "warning",
                    "code": "scheduled_failed",
                    "title": f"定时任务近 7 日失败 {scheduled['failed_7d']} 次",
                    "detail": "到任务中心查看运行记录里的失败原因。",
                }
            )
        alerts.sort(key=lambda item: LEVELS.index(item["level"]))
        return {"items": alerts, "health": live["health"], "generated_at": live["now"]}

    def turns(self, limit=12, offset=0, scope=None):
        """One page of recent turns, newest first, with the tools each one ran.

        Paged rather than scrolled: a clipped row at the bottom of an inner
        scroll box is the ugliest thing a console can do, and it hides how much
        data there actually is.
        """
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        rows = []
        for trace in self._scan(24, limit=2000):
            trace_scope = str(trace.get("scope") or "")
            if scope and trace_scope != scope:
                continue
            steps = trace.get("steps") or ()
            rows.append(
                {
                    "id": trace.get("id"),
                    "at": trace.get("created_at"),
                    "scope": trace_scope or "未知",
                    "outcome": str(trace.get("outcome") or "unknown"),
                    "duration_ms": trace.get("duration_ms"),
                    "steps": len(steps),
                    "tools": [
                        str(step.get("tool") or "")
                        for step in steps
                        if step.get("stage") == "tool" and step.get("outcome") != "running"
                    ],
                }
            )
        rows.sort(key=lambda row: (row["at"] or 0, row["id"] or 0), reverse=True)
        return _page(rows, limit, offset, scope=scope)

    def scopes(self, hours=24):
        """Per-conversation load, so an operator can see which chat is noisy."""
        hours = max(1, min(168, int(hours)))
        buckets: dict[str, dict] = {}
        for trace in self._scan(hours):
            scope = str(trace.get("scope") or "未知")
            row = buckets.setdefault(
                scope, {"scope": scope, "turns": 0, "replies": 0, "errors": 0, "_durations": []}
            )
            row["turns"] += 1
            row["replies"] += trace.get("outcome") == "sent"
            row["errors"] += trace.get("outcome") == "error"
            duration = trace.get("duration_ms")
            if isinstance(duration, (int, float)):
                row["_durations"].append(duration)
        items = []
        for row in buckets.values():
            durations = sorted(row.pop("_durations"))
            row["p95_ms"] = durations[max(0, -(-len(durations) * 95 // 100) - 1)] if durations else None
            row["error_ratio"] = round(row["errors"] / row["turns"], 4) if row["turns"] else None
            items.append(row)
        items.sort(key=lambda row: (-row["turns"], row["scope"]))
        return {"items": items[:12], "total": len(items), "hours": hours}

    def tail(self, limit=20, level=None, q=None, offset=0):
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))
        if self.logs is None:
            return {"available": False, "items": [], "levels": [], "total": 0, "offset": 0, "limit": limit, "pages": 1}
        items = self.logs.tail(500)
        levels = sorted({str(item.get("level") or "") for item in items} - {""})
        if level:
            items = [item for item in items if item.get("level") == level]
        if q:
            needle = str(q).casefold()
            items = [
                item
                for item in items
                if needle in str(item.get("message") or "").casefold()
                or needle in str(item.get("logger") or "").casefold()
            ]
        return {"available": True, "levels": levels, **_page(items, limit, offset)}
