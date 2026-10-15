"""Filesystem adapter for canonical Wiki Markdown snapshots."""

from __future__ import annotations

import json
import re
from pathlib import Path

from qqbot.agent.memory.wiki.page import WikiEntry, WikiLink, WikiPage, WikiRelation, WikiSlang, WikiTopic


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise ValueError("Wiki page is missing frontmatter")
    _, raw, body = text.split("---\n", 2)
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        try:
            result[key.strip()] = str(json.loads(value))
        except json.JSONDecodeError:
            result[key.strip()] = value
    return result, body


class MarkdownWikiStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def load(self) -> list[WikiPage]:
        pages: list[WikiPage] = []
        if not self.root.exists():
            return pages
        for path in sorted(self.root.rglob("*.md")):
            pages.append(self._parse(path))
        return pages

    def _parse(self, path: Path) -> WikiPage:
        metadata, body = _frontmatter(path.read_text(encoding="utf-8"))
        page_id = metadata["page_id"]
        entries: list[WikiEntry] = []
        topics: list[WikiTopic] = []
        slang: list[WikiSlang] = []
        links: list[WikiLink] = []
        relations: list[WikiRelation] = []
        section = ""
        lines = body.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("## "):
                section = line[3:].strip()
                continue
            if not line.startswith("- ") or line == "- 暂无" or line == "- 暂无已发布事实":
                continue
            value = line[2:]
            if section == "事实":
                match = re.match(r"^(.*?) \(fact_type=(.*?), confidence=([0-9.]+), observed_at=(\d+)(?:, source_event_id: (.*?))?\)$", value)
                if match:
                    hint = ""
                    policy = "soft_echo"
                    if index + 1 < len(lines) and lines[index + 1].startswith("  - 表达提示："):
                        hint = lines[index + 1].split("：", 1)[1]
                    if index + 2 < len(lines) and "提及策略" in lines[index + 2]:
                        policy = lines[index + 2].split("`", 2)[1]
                    entries.append(WikiEntry(
                        f"parsed-{len(entries)}", match.group(1), fact_type=match.group(2),
                        persona_hint=hint, mention_policy=policy,
                        source_event_id=match.group(5), observed_at=int(match.group(4)),
                        confidence=float(match.group(3)),
                    ))
            elif section == "主题":
                match = re.match(r"^\*\*(.*?)\*\*：(.*?)（最近 (\d+)，出现 (\d+) 次）$", value)
                if match:
                    topics.append(WikiTopic(f"parsed-{len(topics)}", match.group(1), match.group(2), mention_count=int(match.group(4)), last_seen_at=int(match.group(3))))
            elif section == "黑话":
                match = re.match(r"^\*\*(.*?)\*\*：(.*?)（置信度 ([0-9.]+)）$", value)
                if match:
                    slang.append(WikiSlang(match.group(1), match.group(2), confidence=float(match.group(3))))
            elif section == "关联":
                match = re.match(r"^\[(.*?)\]\((.*?)\)（(.*)，置信度 ([0-9.]+)）$", value)
                if match:
                    target = str((path.parent / match.group(2)).with_suffix("").relative_to(self.root)).replace("\\", "/")
                    links.append(WikiLink(page_id, target, match.group(3), match.group(1), float(match.group(4))))
            elif section == "关系":
                match = re.match(r"^(.*?) → (.*?)（(.*?)，分数 ([0-9.]+)，时间 (\d+)）$", value)
                if match:
                    relations.append(WikiRelation(f"parsed-{len(relations)}", match.group(1), match.group(2), match.group(3), float(match.group(4)), int(match.group(5))))
        return WikiPage(
            page_id=page_id,
            scope=metadata["scope"],
            entity_id=metadata["entity_id"],
            title=metadata["title"],
            summary=metadata.get("summary", ""),
            entries=tuple(entries),
            topics=tuple(topics),
            slang=tuple(slang),
            links=tuple(links),
            relations=tuple(relations),
            version=metadata.get("version", ""),
        )

