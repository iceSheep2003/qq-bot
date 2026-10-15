"""Render accepted Wiki entities as ordinary Markdown files."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from .links import markdown_link
from .page import WikiEntry, WikiPage

_SAFE_SCALAR = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")


def _yaml_scalar(value: object) -> str:
    text = str(value or "")
    return text if _SAFE_SCALAR.fullmatch(text) else json.dumps(text, ensure_ascii=False)


def _entry_order(entries: tuple[WikiEntry, ...]) -> list[WikiEntry]:
    return sorted(entries, key=lambda item: (-item.observed_at, -item.updated_at, item.entry_id))


class WikiWriter:
    """Project a published page; it never calls a model or reads the database."""

    def render(self, page: WikiPage) -> str:
        lines = [
            "---",
            f"title: {_yaml_scalar(page.title)}",
            f"page_id: {_yaml_scalar(page.page_id)}",
            f"scope: {_yaml_scalar(page.scope)}",
            f"entity_id: {_yaml_scalar(page.entity_id)}",
            f"version: {json.dumps(page.version, ensure_ascii=False)}",
            f"summary: {_yaml_scalar(page.summary)}",
            "---",
            "",
            f"# {page.title}",
            "",
            page.summary,
            "",
            "## 事实",
            "",
        ]
        for entry in _entry_order(page.entries):
            metadata = [
                f"fact_type={_yaml_scalar(entry.fact_type)}",
                f"confidence={entry.confidence:.2f}",
                f"observed_at={entry.observed_at}",
            ]
            if entry.source_event_id:
                metadata.append(f"source_event_id: {_yaml_scalar(entry.source_event_id)}")
            lines.append(f"- {entry.claim} ({', '.join(metadata)})")
            if entry.persona_hint:
                lines.append(f"  - 表达提示：{entry.persona_hint}")
            lines.append(f"  - 提及策略：`{entry.mention_policy}`")
        if not page.entries:
            lines.append("- 暂无已发布事实")

        lines.extend(["", "## 主题", ""])
        for topic in sorted(page.topics, key=lambda item: (-item.last_seen_at, item.topic_id)):
            lines.append(f"- **{topic.name}**：{topic.summary}（最近 {topic.last_seen_at}，出现 {topic.mention_count} 次）")
        if not page.topics:
            lines.append("- 暂无")

        lines.extend(["", "## 黑话", ""])
        for slang in sorted(page.slang, key=lambda item: (-item.last_seen_at, item.term)):
            lines.append(f"- **{slang.term}**：{slang.definition}（置信度 {slang.confidence:.2f}）")
        if not page.slang:
            lines.append("- 暂无")

        links = [link for link in page.links if link.source_page_id == page.page_id]
        lines.extend(["", "## 关联", ""])
        for link in links:
            lines.append(
                f"- {markdown_link(page.page_id, link.target_page_id, link.label or link.target_page_id)}"
                f"（{link.kind}，置信度 {link.confidence:.2f}）"
            )
        if not links:
            lines.append("- 暂无")
        lines.extend(["", "## 关系", ""])
        for relation in sorted(page.relations, key=lambda item: (-item.observed_at, item.relation_id)):
            lines.append(
                f"- {relation.source_entity_id} → {relation.target_entity_id}"
                f"（{relation.kind}，分数 {relation.score:.2f}，时间 {relation.observed_at}）"
            )
        if not page.relations:
            lines.append("- 暂无")
        lines.append("")
        return "\n".join(lines)

    def write(self, root: Path, page: WikiPage) -> Path:
        relative = PurePosixPath(f"{page.page_id}.md")
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Wiki page id must stay below the Wiki root")
        root = Path(root)
        target = (root / Path(*relative.parts)).resolve()
        root_resolved = root.resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError("Wiki page path escapes the Wiki root") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        content = self.render(page)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

