"""Routing board: which model serves each link, and which behaviour route runs.

Two things are deliberately *not* reimplemented here. The effective behaviour
route follows the runtime's own fallback (a mode with no usable snapshot is
``off``), and the snapshot itself is loaded by the same loader the bot uses when
the operator asks for a probe. A board that guessed either would be the first
thing to lie during an incident.

Every switch writes the same ``.env`` the process reads at start-up, so a change
is reported as ``restart_required`` rather than pretending to be live.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

MODES = ("off", "hint", "graph")
MODE_LABELS = {
    "off": "关闭：模型独立决策",
    "hint": "提示：图谱补充参考",
    "graph": "图谱：图谱定调，模型表达",
}
SWITCHABLE = (
    "BOT_BEHAVIOR_MODE",
    "BOT_BEHAVIOR_GRAPH_PATH",
    "BOT_BEHAVIOR_CALIBRATION_PATH",
    "BOT_MEMORY_GRAPH_ENABLED",
    "BOT_WIKI_ENABLED",
    "BOT_WIKI_ROOT",
)
MARKERS = ("manifest.json", "graph.json", "snapshot.json")
MAX_ARTIFACTS = 50


class RouteConsoleQuery:
    def __init__(self, store, root=None, opener=None, clock=time.monotonic):
        self.store = store
        self.root = Path(root or ".").resolve()
        self.open_url = opener or urllib.request.urlopen
        self.clock = clock

    # -- reading ---------------------------------------------------------

    def _values(self):
        return {
            key: str(entry.get("value") or "")
            for key, entry in self.store.read().items()
        }

    def _inside(self, raw):
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            return None
        return resolved if resolved.is_relative_to(self.root) else None

    def _artifact(self, path: Path):
        summary = {}
        for marker in MARKERS:
            target = path / marker
            if not target.is_file():
                continue
            summary["marker"] = marker
            summary["bytes"] = target.stat().st_size
            if marker == "manifest.json" and target.stat().st_size <= 512 * 1024:
                try:
                    manifest = json.loads(target.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    manifest = None
                if isinstance(manifest, dict):
                    summary["manifest"] = {
                        key: (len(manifest[key]) if key == "missing_cases" and isinstance(manifest[key], list) else manifest[key])
                        for key in (
                            "status",
                            "schema_version",
                            "expected_train_cases",
                            "proposals",
                            "missing_cases",
                            "duplicates_removed",
                            "failed_cases",
                        )
                        if key in manifest
                    }
            break
        if not summary:
            return None
        try:
            relative = path.resolve().relative_to(self.root)
        except ValueError:
            return None
        return {
            "name": path.name,
            "path": relative.as_posix(),
            "modified_at": int(path.stat().st_mtime),
            **summary,
        }

    def artifacts(self, limit=20):
        if not 1 <= int(limit) <= MAX_ARTIFACTS:
            raise ValueError("limit must be between 1 and 50")
        base = self.root / "data/private/derived"
        if not base.is_dir():
            return {"root": "data/private/derived", "exists": False, "items": []}
        items = []
        for child in sorted(base.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            found = self._artifact(child)
            if found:
                items.append(found)
        items.sort(key=lambda item: (-item["modified_at"], item["name"]))
        return {
            "root": "data/private/derived",
            "exists": True,
            "items": items[: int(limit)],
            "total": len(items),
        }

    def overview(self):
        values = self._values()
        mode = (values.get("BOT_BEHAVIOR_MODE") or "off").strip().lower()
        if mode not in MODES:
            mode = "off"
        snapshot = values.get("BOT_BEHAVIOR_GRAPH_PATH", "").strip()
        calibration = values.get("BOT_BEHAVIOR_CALIBRATION_PATH", "").strip()
        resolved = self._inside(snapshot) if snapshot else None
        exists = bool(resolved and resolved.exists())
        effective = mode if mode != "off" and exists else "off"
        if mode != "off" and not exists:
            reason = "已选择图谱模式，但快照路径为空或不存在，运行时回落到关闭"
        elif mode == "off":
            reason = "图谱路由关闭，回复完全由模型决定"
        else:
            reason = f"图谱路由生效：{MODE_LABELS[mode]}"
        wiki_root = values.get("BOT_WIKI_ROOT", "").strip()
        wiki_resolved = self._inside(wiki_root) if wiki_root else None
        calibration_resolved = self._inside(calibration) if calibration else None
        return {
            "llm": self._llm(values),
            "pipeline": self._pipeline(values, effective, exists),
            "graph": {
                # Knowledge-graph routing is an experiment that this release
                # does not turn on. The board still reports what the .env says,
                # but it must not offer it as a working switch.
                "experimental": True,
                "available": False,
                "mode": mode,
                "mode_label": MODE_LABELS[mode],
                "modes": [{"id": item, "label": MODE_LABELS[item]} for item in MODES],
                "effective": effective,
                "snapshot": snapshot,
                "snapshot_exists": exists,
                "calibration": calibration,
                "calibration_exists": bool(calibration_resolved and calibration_resolved.exists()),
                "reason": reason,
            },
            "wiki": {
                # The distilled Wiki is the memory backbone now: it replaces the
                # slang and memory extraction paths, so it is not a feature that
                # gets switched on and off per deployment.
                "always_on": True,
                "enabled": True,
                "root": wiki_root,
                "root_exists": bool(wiki_resolved and wiki_resolved.is_dir()),
                "replaces": ["黑话提取", "长期记忆条目", "主题归纳"],
            },
            "memory_graph": values.get("BOT_MEMORY_GRAPH_ENABLED", "").lower() == "true",
            "artifacts": self.artifacts(limit=12),
            "restart_required": True,
        }

    def _llm(self, values):
        def link(identifier, label, model, base, note):
            return {
                "id": identifier,
                "label": label,
                "model": model or "",
                "base_url": base or "",
                "configured": bool(model),
                "note": note,
            }

        return [
            link(
                "chat",
                "主对话",
                values.get("BOT_MODEL_NAME", ""),
                values.get("BOT_MODEL_BASE_URL", ""),
                f"推理强度 {values.get('BOT_MODEL_REASONING_EFFORT') or '默认'} · 上限 {values.get('BOT_MODEL_MAX_TOKENS') or '默认'} tokens",
            ),
            link(
                "vision",
                "图片理解",
                values.get("BOT_VISION_MODEL", ""),
                values.get("BOT_VISION_BASE_URL", ""),
                "未配置时跳过识图",
            ),
            link(
                "asr",
                "语音识别",
                values.get("BOT_ASR_MODEL", ""),
                values.get("BOT_ASR_BASE_URL", ""),
                "未配置时语音消息只按文本处理",
            ),
            link(
                "tts",
                "语音合成",
                values.get("BOT_TTS_MODEL", ""),
                "",
                f"服务 {values.get('BOT_TTS_PROVIDER') or '未配置'} · 音色 {values.get('BOT_TTS_VOICE') or '默认'}",
            ),
        ]

    def _pipeline(self, values, effective, exists):
        return [
            {
                "id": "admission",
                "label": "准入",
                "enabled": values.get("BOT_REPLY_POLICY_ENABLED", "").lower() == "true",
                "detail": "读空气策略决定是否参与这一轮",
            },
            {
                "id": "memory",
                "label": "记忆",
                "enabled": True,
                "detail": "短期窗口 + 长期 Wiki"
                + ("（Wiki 已启用）" if values.get("BOT_WIKI_ENABLED", "").lower() == "true" else "（Wiki 未启用）"),
            },
            {
                "id": "route",
                "label": "行为路由",
                "enabled": effective != "off",
                "detail": MODE_LABELS[effective] if effective != "off" else "模型独立决策",
            },
            {
                "id": "model",
                "label": "模型",
                "enabled": bool(values.get("BOT_MODEL_NAME")),
                "detail": values.get("BOT_MODEL_NAME") or "未配置",
            },
            {
                "id": "tools",
                "label": "工具",
                "enabled": True,
                "detail": "Skill 提供的工具在模型请求时执行",
            },
            {
                "id": "delivery",
                "label": "投递",
                "enabled": True,
                "detail": "文字 / 语音 / 表情包由 planner 决定",
            },
        ]

    # -- writing ---------------------------------------------------------

    def apply(self, changes):
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes 必须是非空对象")
        unknown = sorted(set(changes) - set(SWITCHABLE))
        if unknown:
            raise ValueError(f"不支持的链路开关：{', '.join(unknown)}")
        normalized = {key: str(value).strip() for key, value in changes.items()}
        if "BOT_BEHAVIOR_MODE" in normalized:
            mode = normalized["BOT_BEHAVIOR_MODE"].lower()
            if mode not in MODES:
                raise ValueError("行为路由只能是 off / hint / graph")
            normalized["BOT_BEHAVIOR_MODE"] = mode
        for key in ("BOT_BEHAVIOR_GRAPH_PATH", "BOT_BEHAVIOR_CALIBRATION_PATH"):
            raw = normalized.get(key)
            if not raw:
                continue
            resolved = self._inside(raw)
            if resolved is None or not resolved.exists():
                raise ValueError(f"{key} 指向的文件不存在或在项目目录之外")
        updated = self.store.update(normalized)
        return {"updated": updated, "restart_required": bool(updated)}

    def probe(self):
        """Load the configured snapshot exactly as the runtime would."""
        values = self._values()
        mode = (values.get("BOT_BEHAVIOR_MODE") or "off").strip().lower()
        try:
            from qqbot.agent.cognition.experience.runtime import (
                configured_behavior_experience,
            )
        except Exception as error:  # pragma: no cover - import guard
            return {"ok": False, "mode": mode, "detail": f"无法加载图谱读取器：{error}"}
        settings = {
            key: values.get(key, "")
            for key in (
                "BOT_BEHAVIOR_GRAPH_PATH",
                "BOT_BEHAVIOR_CALIBRATION_PATH",
                "BOT_BEHAVIOR_PRIOR_WEIGHT",
                "BOT_BEHAVIOR_MAX_OPTIONS",
                "BOT_BEHAVIOR_MAX_CASES",
                "BOT_BEHAVIOR_CHAR_BUDGET",
            )
        }
        experience = configured_behavior_experience(settings=settings)
        if experience is None:
            return {
                "ok": False,
                "mode": mode,
                "detail": "快照不可用：路径为空、文件缺失或格式错误，运行时将回落到关闭",
            }
        return {
            "ok": True,
            "mode": mode,
            "detail": f"快照可用，路由模式 {mode}",
            "snapshot": str(getattr(experience.retriever, "snapshot", "")),
        }

    def probe_model(self, timeout=15.0):
        """One real completion request.

        A model that is *configured* is not a model that *works*: the previous
        endpoint answered nothing at all while the console happily reported it
        as configured. This spends one token to find out which it is.
        """
        values = self._values()
        base = values.get("BOT_MODEL_BASE_URL", "").strip().rstrip("/")
        model = values.get("BOT_MODEL_NAME", "").strip()
        read_secret = getattr(self.store, "secret", None)
        key = read_secret("BOT_MODEL_API_KEY").strip() if callable(read_secret) else ""
        if not base or not model:
            return {"ok": False, "detail": "模型名或地址为空，Bot 无法生成回复"}
        if not key:
            return {"ok": False, "detail": "缺少 API Key，先在模型配置里填写再测"}
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "stream": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{base}/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        started = self.clock()
        try:
            with self.open_url(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            return {
                "ok": False,
                "status": error.code,
                "model": model,
                "base_url": base,
                "detail": self._http_error(error),
            }
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            return {"ok": False, "model": model, "base_url": base, "detail": f"连不上 {base}：{reason}"}
        elapsed = int((self.clock() - started) * 1000)
        try:
            body = json.loads(raw or "{}")
        except ValueError:
            return {"ok": False, "model": model, "base_url": base, "detail": "接口返回的不是 JSON，检查地址是否少写了 /v1"}
        served = str(body.get("model") or model) if isinstance(body, dict) else model
        return {
            "ok": True,
            "model": served,
            "base_url": base,
            "latency_ms": elapsed,
            "detail": f"连通，实际服务模型 {served}，{elapsed} ms",
        }

    @staticmethod
    def _http_error(error):
        raw = ""
        try:
            raw = error.read().decode("utf-8", "replace")
        except Exception:  # a broken error body must not hide the status code
            raw = ""
        message = raw.strip()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            detail = parsed.get("error")
            if isinstance(detail, dict):
                message = str(detail.get("message") or message)
            elif isinstance(detail, str):
                message = detail
        hint = {
            401: "（密钥无效）",
            402: "（余额不足）",
            403: "（没有权限）",
            404: "（模型名或路径不存在）",
            429: "（触发限流）",
        }.get(error.code, "")
        return f"HTTP {error.code}{hint}：{message[:200]}"
