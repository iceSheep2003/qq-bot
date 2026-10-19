"""Skill management: create, edit, switch off and archive.

The console reads the skills *directory*, not the running catalogue. The
catalogue only holds what the extension allow-list admitted, so a view built on
it could never answer the first question anyone asks when a skill stops firing:
"why is it gone?" A directory view can say "installed but its extension is off"
instead of silently omitting the row.

Format ownership stays in :mod:`qqbot.agent.skills.catalog`. This module parses
and renders through those helpers rather than keeping a second dialect that
would drift from what the runtime reads.

Nothing here executes a skill: a ``SKILL.md`` is still text. The console can
only write text into the same place the loader already reads.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from qqbot.agent.skills.catalog import (
    MAX_TRIGGER_CHARS,
    parse_document,
    read_disabled,
    render_document,
    write_disabled,
)

AUDIENCES = ("group", "private")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
ARCHIVE_DIR = ".archive"
ADAPTER_FILE = "bot-adapter.json"
MAX_DESCRIPTION = 300
MAX_TRIGGERS = 24
MAX_BODY = 60000
STATES = ("enabled", "disabled", "unavailable", "broken")


class SkillAdmin:
    def __init__(self, root, catalog=None, calls=None, extensions=None, clock=time.time):
        self.root = Path(root)
        self.catalog = catalog
        self.calls = dict(calls or {})
        self.extensions = dict(extensions or {})
        self.clock = clock

    # -- reading ---------------------------------------------------------

    def _folders(self):
        if not self.root.is_dir():
            return []
        return [
            path
            for path in sorted(self.root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and not path.name.startswith(".")
        ]

    def _folder_for(self, name: str):
        for folder in self._folders():
            if folder.name == name:
                return folder
            document = folder / "SKILL.md"
            if document.is_file():
                try:
                    fields, _ = parse_document(document.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if str(fields.get("name") or "").strip().strip("\"'") == name:
                    return folder
        return None

    def _allow(self):
        """The extension allow-list, or ``None`` when it is not knowable."""
        return getattr(self.catalog, "enabled_names", None)

    def _unavailable_reason(self, name: str) -> str:
        extension = self.extensions.get(name)
        if extension:
            return f"所属扩展 {extension} 未启用（BOT_EXTENSIONS）"
        if name == "private-chat":
            return "私聊通道未开启（BOT_PRIVATE_ENABLED=false）"
        return "不在当前装配白名单里，检查扩展配置"

    def _row(self, folder: Path, disabled) -> dict:
        document = folder / "SKILL.md"
        issues: list[str] = []
        fields: dict[str, str] = {}
        body = ""
        if not document.is_file():
            issues.append("目录里没有 SKILL.md")
        else:
            try:
                fields, body = parse_document(document.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                issues.append(str(error))
        name = str(fields.get("name") or "").strip().strip("\"'") or folder.name
        description = str(fields.get("description") or "").strip().strip("\"'")
        if document.is_file() and not issues and not description:
            issues.append("frontmatter 缺少 description，运行时会跳过")
        try:
            audiences = _audiences(fields.get("audiences"))
        except ValueError as error:
            issues.append(str(error))
            audiences = ()
        triggers = _triggers(fields.get("triggers"))
        adapter = _adapter(folder)
        if issues:
            state, reason = "broken", issues[0]
        elif name in disabled:
            state, reason = "disabled", "已在本机停用"
        elif self._allow() is not None and name not in self._allow():
            state, reason = "unavailable", self._unavailable_reason(name)
        else:
            state, reason = "enabled", ""
        return {
            "name": name,
            "dir": folder.name,
            "description": description,
            "triggers": list(triggers),
            "priority": _int(fields.get("priority")),
            "always_on": bool(adapter.get("always_on")) or _truthy(fields.get("always_on")),
            "audiences": list(audiences),
            "body_chars": len(body),
            "calls": int(self.calls.get(name, 0)),
            "state": state,
            "reason": reason,
            "issues": issues,
            "adapter": bool(adapter),
            "extension": self.extensions.get(name, ""),
            "modified_at": int(document.stat().st_mtime) if document.is_file() else 0,
        }

    def overview(self):
        disabled = read_disabled(self.root)
        rows = [self._row(folder, disabled) for folder in self._folders()]
        seen: dict[str, int] = {}
        for row in rows:
            seen[row["name"]] = seen.get(row["name"], 0) + 1
        for row in rows:
            if seen[row["name"]] > 1:
                row["issues"] = [*row["issues"], "有多个目录声明了同一个 name"]
                if row["state"] == "enabled":
                    row["state"] = "broken"
                    row["reason"] = "重名：运行时会保留先读到的那一个"
        rows.sort(key=lambda row: (STATES.index(row["state"]), -row["calls"], row["name"]))
        counts = {state: sum(1 for row in rows if row["state"] == state) for state in STATES}
        return {
            "root": self.root.name,
            "state_path": f"{self.root.name}/{'.state.json'}",
            "available": self.root.is_dir(),
            "total": len(rows),
            "counts": counts,
            "calls_7d": sum(row["calls"] for row in rows),
            "allow_list_known": self._allow() is not None,
            "skills": rows,
            "archived": self._archived(),
        }

    def _archived(self):
        folder = self.root / ARCHIVE_DIR
        if not folder.is_dir():
            return []
        items = []
        for path in sorted(folder.iterdir(), key=lambda item: item.name, reverse=True)[:20]:
            if path.is_dir():
                items.append({"name": path.name, "archived_at": int(path.stat().st_mtime)})
        return items

    def detail(self, name: str):
        folder = self._folder_for(name)
        if folder is None:
            return None
        row = self._row(folder, read_disabled(self.root))
        document = folder / "SKILL.md"
        try:
            raw = document.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        try:
            fields, body = parse_document(raw)
        except ValueError:
            fields, body = {}, raw
        adapter = _adapter(folder)
        return {
            **row,
            "raw": raw,
            "body": body,
            "fields": {key: value for key, value in fields.items()},
            "adapter_config": adapter,
            "path": f"{self.root.name}/{folder.name}/SKILL.md",
        }

    # -- writing ---------------------------------------------------------

    def _normalize(self, payload) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("提交内容必须是对象")
        description = str(payload.get("description") or "").strip()
        if not description:
            raise ValueError("description 不能为空，运行时会跳过没有描述的 Skill")
        if len(description) > MAX_DESCRIPTION:
            raise ValueError(f"description 超过 {MAX_DESCRIPTION} 字")
        raw_triggers = payload.get("triggers") or []
        if isinstance(raw_triggers, str):
            raw_triggers = re.split(r"[,，]", raw_triggers)
        if not isinstance(raw_triggers, (list, tuple)):
            raise ValueError("triggers 必须是数组或逗号分隔字符串")
        triggers: list[str] = []
        for item in raw_triggers:
            token = str(item).strip()
            if not token:
                continue
            if len(token) > MAX_TRIGGER_CHARS:
                raise ValueError(f"触发词「{token[:8]}…」超过 {MAX_TRIGGER_CHARS} 字")
            if token not in triggers:
                triggers.append(token)
        if len(triggers) > MAX_TRIGGERS:
            raise ValueError(f"触发词最多 {MAX_TRIGGERS} 个")
        try:
            priority = int(payload.get("priority") or 0)
        except (TypeError, ValueError):
            raise ValueError("priority 必须是整数") from None
        if not -100 <= priority <= 100:
            raise ValueError("priority 必须在 -100 到 100 之间")
        audiences = payload.get("audiences") or ["group"]
        if isinstance(audiences, str):
            audiences = [audiences]
        audiences = [str(item).strip() for item in audiences if str(item).strip()]
        if not audiences:
            raise ValueError("至少要选一个作用域")
        unknown = [item for item in audiences if item not in AUDIENCES]
        if unknown:
            raise ValueError(f"未知作用域：{', '.join(unknown)}")
        body = str(payload.get("body") or "").strip()
        if not body:
            raise ValueError("正文不能为空，否则这个 Skill 不会给模型任何指令")
        if len(body) > MAX_BODY:
            raise ValueError(f"正文超过 {MAX_BODY} 字")
        return {
            "description": description,
            "triggers": triggers,
            "priority": priority,
            "audiences": audiences,
            "always_on": bool(payload.get("always_on")),
            "body": body,
        }

    def _guard_adapter(self, folder: Path, body: str) -> None:
        """Refuse an edit that would silently orphan an adapter's sections."""
        adapter = _adapter(folder)
        for section in adapter.get("sections") or ():
            if f"## {section}" not in body:
                raise ValueError(
                    f"这个 Skill 由 {ADAPTER_FILE} 接管，只读取「{section}」小节；"
                    f"正文里必须保留 `## {section}` 标题"
                )

    def _write(self, folder: Path, text: str) -> None:
        target = folder / "SKILL.md"
        temporary = folder / "SKILL.md.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)

    def _reload(self) -> bool:
        reload = getattr(self.catalog, "reload", None)
        if not callable(reload):
            return False
        try:
            reload()
        except Exception:  # a broken skill must not take the console down
            return False
        return True

    def save(self, name: str, payload):
        folder = self._folder_for(name)
        if folder is None:
            raise KeyError(name)
        normalized = self._normalize(payload)
        self._guard_adapter(folder, normalized["body"])
        text = render_document(name=name, **normalized)
        self._write(folder, text)
        return {"saved": name, "reloaded": self._reload(), "detail": self.detail(name)}

    def create(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("提交内容必须是对象")
        name = str(payload.get("name") or "").strip().lower()
        if not NAME_PATTERN.match(name):
            raise ValueError("名称只能用小写字母、数字、- 和 _，2–41 个字符，且以字母或数字开头")
        if (self.root / name).exists():
            raise ValueError(f"目录 {self.root.name}/{name} 已存在")
        normalized = self._normalize(payload)
        folder = self.root / name
        folder.mkdir(parents=True)
        self._write(folder, render_document(name=name, **normalized))
        return {"created": name, "reloaded": self._reload(), "detail": self.detail(name)}

    def import_document(self, filename: str, text: str):
        """Install a SKILL.md someone uploaded or pasted."""
        raw = str(text or "")
        if not raw.strip():
            raise ValueError("文件是空的")
        try:
            fields, _ = parse_document(raw)
        except ValueError as error:
            raise ValueError(f"{error}；上传的文件必须是完整 SKILL.md") from None
        declared = str(fields.get("name") or "").strip().strip("\"'")
        name = _slug(declared or Path(str(filename or "")).stem or "")
        if not NAME_PATTERN.match(name):
            raise ValueError("无法从文件里得到合法名称，请先在 frontmatter 写 name:")
        if (self.root / name).exists():
            raise ValueError(f"已存在同名 Skill：{name}")
        if not str(fields.get("description") or "").strip():
            raise ValueError("frontmatter 缺少 description")
        folder = self.root / name
        folder.mkdir(parents=True)
        self._write(folder, raw if raw.endswith("\n") else raw + "\n")
        return {"created": name, "reloaded": self._reload(), "detail": self.detail(name)}

    def set_enabled(self, name: str, enabled: bool):
        folder = self._folder_for(name)
        if folder is None:
            raise KeyError(name)
        row = self._row(folder, read_disabled(self.root))
        disabled = set(read_disabled(self.root))
        if enabled:
            disabled.discard(row["name"])
        else:
            disabled.add(row["name"])
        write_disabled(self.root, disabled)
        return {
            "name": row["name"],
            "enabled": bool(enabled),
            "reloaded": self._reload(),
            "runtime_effect": _runtime_effect(row, enabled),
        }

    def archive(self, name: str):
        folder = self._folder_for(name)
        if folder is None:
            raise KeyError(name)
        target_root = self.root / ARCHIVE_DIR
        if not target_root.is_dir():
            target_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        target = target_root / f"{folder.name}-{stamp}"
        shutil.move(str(folder), str(target))
        disabled = set(read_disabled(self.root))
        if name in disabled:
            write_disabled(self.root, disabled - {name})
        return {"archived": target.name, "path": f"{self.root.name}/{ARCHIVE_DIR}/{target.name}", "reloaded": self._reload()}


def _runtime_effect(row, enabled: bool) -> str:
    if row["state"] == "unavailable":
        return "这个 Skill 所属的扩展没有启用，改开关不会让它生效"
    if row["state"] == "broken":
        return "这个 Skill 的文件本身有问题，先修好再启用"
    return "已写入 skills/.state.json，目录已重载" if enabled else "已写入 skills/.state.json，目录已重载"


def _adapter(folder: Path) -> dict:
    path = folder / ADAPTER_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"broken": True}
    return data if isinstance(data, dict) else {"broken": True}


def _audiences(value):
    if value is None or not str(value).strip():
        return ("group",)
    text = str(value).strip().strip("[]")
    items = [item.strip().strip("\"'") for item in re.split(r"[,，]", text) if item.strip()]
    if not items:
        return ("group",)
    unknown = [item for item in items if item not in AUDIENCES]
    if unknown:
        raise ValueError(f"未知作用域：{', '.join(unknown)}")
    return tuple(items)


def _triggers(value):
    if value is None:
        return ()
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return tuple(item.strip().strip("\"'") for item in re.split(r"[,，]", text) if item.strip())


def _int(value) -> int:
    try:
        return int(str(value or "0").strip() or 0)
    except ValueError:
        return 0


def _truthy(value) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _slug(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9_-]+", "-", str(value).strip().lower())
    return re.sub(r"-{2,}", "-", lowered).strip("-")[:41]
