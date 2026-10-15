"""Read-only projection from the legacy SQLite-shaped rows into Wiki pages."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .page import WikiEntry, WikiPage, WikiSlang, WikiTopic
from .update_policy import published_version


def _get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


class LegacyWikiProjector:
    """Compile old memory/topic/slang rows without mutating their stores."""

    def project(
        self,
        *,
        memories=(),
        topics=(),
        slang=(),
    ) -> tuple[WikiPage, ...]:
        pages: dict[str, dict] = {}

        def ensure(page_id: str, scope: str, entity_id: str, title: str) -> dict:
            return pages.setdefault(
                page_id,
                {
                    "page_id": page_id,
                    "scope": scope,
                    "entity_id": entity_id,
                    "title": title,
                    "entries": [],
                    "topics": [],
                    "slang": [],
                },
            )

        for row in memories:
            status = str(_get(row, "status", "active") or "active")
            if status not in {"active", "reviewed", "verified"}:
                continue
            location = self._location(str(_get(row, "scope", "") or ""), str(_get(row, "user_id", "") or ""))
            if location is None:
                continue
            page_id, scope, entity_id, title = location
            target = ensure(page_id, scope, entity_id, title)
            target["entries"].append(
                WikiEntry(
                    entry_id=f"memory-{_get(row, 'id', len(target['entries']))}",
                    claim=str(_get(row, "content", "") or "").strip(),
                    fact_type=str(_get(row, "fact_type", "fact") or "fact"),
                    persona_hint=str(_get(row, "persona_summary", "") or ""),
                    mention_policy=str(_get(row, "mention_policy", "soft_echo") or "soft_echo"),
                    source_event_id=_get(row, "source_event_id"),
                    observed_at=int(_get(row, "created_at", 0) or 0),
                    updated_at=int(_get(row, "updated_at", 0) or 0),
                    confidence=float(_get(row, "confidence", 1.0) or 0.0),
                )
            )

        for row in topics:
            scope = str(_get(row, "scope", "") or "")
            location = self._location(scope, "_group_")
            if location is None or location[1] != "group":
                continue
            page_id, page_scope, entity_id, title = location
            target = ensure(page_id, page_scope, entity_id, title)
            target["topics"].append(
                WikiTopic(
                    topic_id=f"topic-{_get(row, 'id', _get(row, 'canonical_name', ''))}",
                    name=str(_get(row, "canonical_name", "") or "").strip(),
                    summary=str(_get(row, "summary", "") or ""),
                    mention_count=int(_get(row, "mention_count", 0) or 0),
                    last_seen_at=int(_get(row, "last_seen_at", 0) or 0),
                )
            )

        for row in slang:
            scope = str(_get(row, "scope", "") or "")
            location = self._location(scope, "_group_")
            meaning = str(
                _get(row, "meaning", "")
                or _get(row, "context_meaning", "")
                or _get(row, "standalone_meaning", "")
                or ""
            ).strip()
            if location is None or location[1] != "group" or not meaning:
                continue
            if str(_get(row, "status", "candidate") or "candidate") == "rejected":
                continue
            page_id, page_scope, entity_id, title = location
            target = ensure(page_id, page_scope, entity_id, title)
            target["slang"].append(
                WikiSlang(
                    term=str(_get(row, "term", "") or "").strip(),
                    definition=meaning,
                    confidence=float(_get(row, "confidence", 0.0) or 0.0),
                    last_seen_at=int(_get(row, "last_seen", 0) or 0),
                )
            )

        result: list[WikiPage] = []
        for data in pages.values():
            page = WikiPage(
                page_id=data["page_id"],
                scope=data["scope"],
                entity_id=data["entity_id"],
                title=data["title"],
                entries=tuple(data["entries"]),
                topics=tuple(data["topics"]),
                slang=tuple(data["slang"]),
            )
            snapshot = {
                "page_id": page.page_id,
                "entries": [entry.__dict__ for entry in page.entries],
                "topics": [topic.__dict__ for topic in page.topics],
                "slang": [item.__dict__ for item in page.slang],
            }
            result.append(replace(page, version=published_version(snapshot)))
        return tuple(sorted(result, key=lambda page: page.page_id))

    @staticmethod
    def _location(scope: str, user_id: str) -> tuple[str, str, str, str] | None:
        if scope.startswith("group:"):
            group_id = scope.split(":", 1)[1]
            if not group_id:
                return None
            if user_id == "_group_":
                return f"groups/{group_id}", "group", "_group_", f"群 {group_id}"
            return (
                f"groups/{group_id}/members/{user_id}",
                "group_member",
                user_id,
                f"用户 {user_id}",
            )
        if scope.startswith("private:") and user_id:
            return f"private/{user_id}", "private", user_id, f"私聊 {user_id}"
        return None

