"""Owner-facing scheduled-job use cases.

The WebUI talks to this application service, never to SQLite or a handler.
The JSON file remains the source of truth and every mutation is followed by
the scheduler's normal validation/reconciliation path.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .registry import DEFAULT_ACTION, JobSuggestion
from .scheduler import Scheduler, next_occurrence


class ScheduleFileRepository:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()

    def read(self) -> list[dict[str, Any]]:
        with self._lock:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            jobs = raw.get("jobs", [])
            if not isinstance(jobs, list) or not all(isinstance(item, dict) for item in jobs):
                raise ValueError("schedules.json 的 jobs 必须是对象数组")
            return [dict(item) for item in jobs]

    def write(self, jobs: list[dict[str, Any]]) -> None:
        """Replace the config atomically; a crash cannot leave half a JSON file."""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump({"jobs": jobs}, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
                raise


class ScheduleManagementService:
    def __init__(
        self,
        scheduler: Scheduler,
        runner,
        path: Path,
        allowed_groups: frozenset[str],
        suggestions: Iterable[tuple[str, JobSuggestion]],
    ):
        self.scheduler = scheduler
        self.runner = runner
        self.files = ScheduleFileRepository(path)
        self.allowed_groups = allowed_groups
        self.suggestions = tuple(suggestions)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mutation_lock = threading.RLock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _sync(self) -> None:
        self.scheduler.sync_config(self.files.path, self.allowed_groups, self.suggestions)

    def _normalize(self, body: dict[str, Any], *, existing_id: str | None = None) -> dict[str, Any]:
        key = str(body.get("id", existing_id or "")).strip()
        if not key or len(key) > 80 or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_@" for ch in key):
            raise ValueError("任务 ID 只能包含字母、数字、-、_、@，且不能超过 80 个字符")
        if existing_id is not None and key != existing_id:
            raise ValueError("任务 ID 创建后不可修改")
        group_id = str(body.get("group_id", "")).strip()
        if group_id not in self.allowed_groups:
            raise ValueError("目标群不在白名单中")
        kind = str(body.get("kind", "")).strip()
        value = str(body.get("value", "")).strip()
        action = str(body.get("action", DEFAULT_ACTION)).strip() or DEFAULT_ACTION
        prompt = str(body.get("prompt", "")).strip()
        payload = body.get("payload", {})
        self.scheduler._validate_schedule(kind, value, prompt, action, key, payload)
        now = int(time.time())
        if kind == "at":
            next_occurrence(kind, value, after=now, tz_name=self.scheduler.tz_name)
        else:
            next_occurrence(kind, value, after=now, tz_name=self.scheduler.tz_name)
        return {
            "id": key,
            "name": str(body.get("name", key)).strip()[:100] or key,
            "description": str(body.get("description", "")).strip()[:300],
            "group_id": group_id,
            "kind": kind,
            "value": value,
            "action": action,
            "prompt": prompt,
            "payload": payload,
            "enabled": bool(body.get("enabled", True)),
        }

    def jobs(self) -> list[dict[str, Any]]:
        configured = {item["id"]: item for item in self.files.read() if item.get("id")}
        configured_signatures = {
            (str(item.get("group_id", "")), str(item.get("action", DEFAULT_ACTION)), str(item.get("value", "")))
            for item in configured.values()
        }
        current_suggestions = {
            (f"{item.id}@{group_id}", group_id, action, item.value)
            for action, item in self.suggestions
            for group_id in self.allowed_groups
        }
        result = []
        for row in self.scheduler.jobs.all_jobs():
            item = dict(row)
            if item["config_key"] not in configured:
                signature = (item["config_key"], item["group_id"], item["action"], item["schedule_value"])
                if signature not in current_suggestions:
                    continue  # stale suggestion; history remains queryable
                if (item["group_id"], item["action"], item["schedule_value"]) in configured_signatures:
                    continue  # configured task supersedes this template row
            meta = configured.get(item["config_key"], {})
            item["name"] = meta.get("name") or item["config_key"]
            item["description"] = meta.get("description", "")
            item["source"] = "config" if item["config_key"] in configured else "suggestion"
            item["expired"] = False
            if item["schedule_kind"] == "at":
                try:
                    moment = datetime.fromisoformat(item["schedule_value"])
                    if moment.tzinfo is None:
                        from zoneinfo import ZoneInfo
                        moment = moment.replace(tzinfo=ZoneInfo(self.scheduler.tz_name))
                    item["expired"] = int(moment.timestamp()) <= int(time.time())
                except ValueError:
                    item["expired"] = True
            result.append(item)
        return sorted(result, key=lambda item: (not bool(item["enabled"]), item["next_run"], item["config_key"]))

    def overview(self) -> dict[str, Any]:
        now = int(time.time())
        jobs = self.jobs()
        runs = self.scheduler.jobs.list_runs(limit=500)
        return {
            "enabled": sum(bool(item["enabled"]) for item in jobs),
            "next_24h": sum(bool(item["enabled"]) and now <= item["next_run"] <= now + 86400 for item in jobs),
            "running": sum(item["status"] == "running" for item in runs),
            "failed_7d": sum(item["status"] == "failed" and item["started_at"] >= now - 604800 for item in runs),
            "timezone": self.scheduler.tz_name,
            "groups": sorted(self.allowed_groups),
            "actions": sorted(self.scheduler.known_actions),
        }

    def runs(self, job_id: int | None = None, limit: int = 100) -> list[dict]:
        return self.scheduler.jobs.list_runs(job_id, limit)

    def preview(self, kind: str, value: str, count: int = 3) -> list[int]:
        cursor = int(time.time())
        result = []
        for _ in range(max(1, min(10, count))):
            cursor = next_occurrence(kind, value, after=cursor, tz_name=self.scheduler.tz_name)
            result.append(cursor)
            if kind == "at":
                break
        return result

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._mutation_lock:
            jobs = self.files.read()
            item = self._normalize(body)
            if any(str(row.get("id")) == item["id"] for row in jobs):
                raise ValueError("任务 ID 已存在")
            jobs.append(item)
            self.files.write(jobs)
            self._sync()
            return item

    def update(self, key: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._mutation_lock:
            jobs = self.files.read()
            index = next((i for i, item in enumerate(jobs) if str(item.get("id")) == key), None)
            if index is None:
                raise KeyError(key)
            item = self._normalize(body, existing_id=key)
            jobs[index] = item
            self.files.write(jobs)
            self._sync()
            return item

    def set_enabled(self, key: str, enabled: bool) -> dict[str, Any]:
        jobs = self.files.read()
        current = next((item for item in jobs if str(item.get("id")) == key), None)
        if current is None:
            raise KeyError(key)
        return self.update(key, {**current, "enabled": enabled})

    def delete(self, key: str) -> None:
        with self._mutation_lock:
            jobs = self.files.read()
            kept = [item for item in jobs if str(item.get("id")) != key]
            if len(kept) == len(jobs):
                raise KeyError(key)
            self.files.write(kept)
            self._sync()

    def run_now(self, job_id: int, requested_by: str = "webui") -> int:
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("任务执行循环尚未就绪")
        reserved = self.scheduler.jobs.reserve_manual_job(job_id, int(time.time()), requested_by)
        if reserved is None:
            raise KeyError(str(job_id))
        job, run_id = reserved
        asyncio.run_coroutine_threadsafe(self.scheduler._execute(self.runner, job), self._loop)
        return run_id

    def suggestions_list(self) -> list[dict[str, Any]]:
        configured = {str(item.get("id")) for item in self.files.read()}
        return [
            {"id": item.id, "action": action, "kind": item.kind, "value": item.value,
             "prompt": item.prompt, "description": item.description, "adopted": item.id in configured}
            for action, item in self.suggestions
        ]
