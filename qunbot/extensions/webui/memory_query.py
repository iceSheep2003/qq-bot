"""Owner-console read model for memory-adjacent bounded contexts.

This adapter deliberately owns no writes.  It composes public memory use cases
with relationship and slang projections into JSON-ready views, so the browser
never needs to understand storage tables or import domain modules.
"""

from __future__ import annotations

import json
from typing import Any


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


class MemoryConsoleQuery:
    def __init__(self, service):
        self.memory = service.agent.memories
        self.memory_store = self.memory.memories
        self.people = service.people
        self.db = service.conversations.db

    def scopes(self) -> list[str]:
        rows = self.db.execute(
            "SELECT scope FROM messages UNION SELECT scope FROM memories "
            "UNION SELECT scope FROM slang_candidates ORDER BY scope"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def overview(self, scope: str | None = None) -> dict:
        memory = self.memory.overview(scope)
        clauses, params = [], []
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        slang = int(self.db.execute(
            "SELECT count(*) FROM slang_candidates" + where, params
        ).fetchone()[0])
        if scope and scope.startswith("group:"):
            group_id = scope.split(":", 1)[1]
            people = int(self.db.execute(
                "SELECT count(*) FROM group_members WHERE group_id=?", (group_id,)
            ).fetchone()[0])
        else:
            people = int(self.db.execute(
                "SELECT count(*) FROM group_members"
            ).fetchone()[0])
        return {
            "scope": scope or "",
            "scopes": self.scopes(),
            "memories": memory,
            "people": people,
            "slang": slang,
        }

    def memories(
        self, scope: str | None = None, *, status: str | None = None,
        query: str = "", limit: int = 100,
    ) -> list[dict]:
        rows = self.memory_store.list_memories(
            scope, status=status or None, limit=min(max(1, limit), 200)
        )
        folded = query.strip().casefold()
        if folded:
            rows = [row for row in rows if folded in str(row.get("content", "")).casefold()]
        return rows

    def memory_detail(self, memory_id: int) -> dict | None:
        item = self.memory.memory_detail(memory_id)
        if item is None:
            return None
        topics = self.db.execute(
            "SELECT t.* FROM memory_topics t JOIN memory_topic_links l "
            "ON l.topic_id=t.id WHERE l.memory_id=? ORDER BY t.last_seen_at DESC",
            (int(memory_id),),
        ).fetchall()
        item["topics"] = [dict(row) for row in topics]
        return item

    def topics(self, scope: str | None = None, limit: int = 100) -> list[dict]:
        return self.memory.topics(scope, min(max(1, limit), 200))

    def topic_detail(self, topic_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM memory_topics WHERE id=?", (int(topic_id),)
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        memories = self.db.execute(
            "SELECT m.id,m.user_id,m.content,m.fact_type,m.confidence,m.status,"
            "l.weight FROM memory_topic_links l JOIN memories m ON m.id=l.memory_id "
            "WHERE l.topic_id=? ORDER BY l.weight DESC,m.updated_at DESC",
            (int(topic_id),),
        ).fetchall()
        item["memories"] = [dict(value) for value in memories]
        return item

    def people_list(self, scope: str | None = None) -> list[dict]:
        if scope and scope.startswith("group:"):
            groups = [scope.split(":", 1)[1]]
        else:
            groups = [str(row[0]) for row in self.db.execute(
                "SELECT DISTINCT group_id FROM group_members ORDER BY group_id"
            ).fetchall()]
        result = []
        for group_id in groups:
            result.extend(self.people.members(group_id))
        result.sort(key=lambda row: (-int(row.get("last_interaction_at") or 0), row.get("user_id", "")))
        return result

    def person_detail(self, group_id: str, user_id: str) -> dict:
        item = self.people.profile(group_id, user_id)
        item["history"] = self.people.history(group_id, user_id, 50)
        scope = f"group:{group_id}"
        item["memories"] = self.memory_store.list_memories(
            scope, subject_user_id=user_id, limit=100
        )
        return item

    def slang(self, scope: str | None = None, limit: int = 100) -> list[dict]:
        params: list[Any] = []
        where = ""
        if scope:
            where = "WHERE scope=?"
            params.append(scope)
        rows = self.db.execute(
            "SELECT * FROM slang_candidates " + where
            + " ORDER BY confidence DESC,occurrences DESC,last_seen DESC LIMIT ?",
            (*params, min(max(1, limit), 200)),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["seen_users"] = _json_list(item.get("seen_users"))
            item["seen_days"] = _json_list(item.get("seen_days"))
            item["samples"] = _json_list(item.get("samples"))
            result.append(item)
        return result

    def extractions(self, scope: str | None = None, limit: int = 100) -> list[dict]:
        return self.memory.extraction_runs(scope, min(max(1, limit), 200))

    def graph(self, scope: str | None = None, limit: int = 120) -> dict:
        """Bounded graph projection; only persisted relationships become edges."""
        limit = min(max(1, int(limit)), 200)
        nodes: dict[str, dict] = {
            "bot": {"id": "bot", "kind": "bot", "label": "Bot", "ref_id": ""}
        }
        links: list[dict] = []
        topic_where, topic_params = ("WHERE scope=?", [scope]) if scope else ("", [])
        for row in self.db.execute(
            "SELECT * FROM memory_topics " + topic_where
            + " ORDER BY last_seen_at DESC LIMIT ?", (*topic_params, limit)
        ).fetchall():
            item = dict(row)
            node_id = f"topic:{item['id']}"
            nodes[node_id] = {
                "id": node_id, "kind": "topic", "label": item["canonical_name"],
                "ref_id": item["id"], "weight": item["mention_count"],
            }

        memory_where, memory_params = ("WHERE scope=?", [scope]) if scope else ("", [])
        for row in self.db.execute(
            "SELECT id,scope,user_id,content,importance,status FROM memories "
            + memory_where + " ORDER BY updated_at DESC LIMIT ?",
            (*memory_params, limit),
        ).fetchall():
            item = dict(row)
            node_id = f"memory:{item['id']}"
            nodes[node_id] = {
                "id": node_id, "kind": "memory", "label": item["content"],
                "ref_id": item["id"], "weight": item["importance"],
                "status": item["status"],
            }
            if item["user_id"] != "_group_":
                person_id = f"person:{item['scope'].split(':', 1)[-1]}:{item['user_id']}"
                if person_id in nodes:
                    links.append({"source": node_id, "target": person_id, "kind": "subject"})

        linked = self.db.execute(
            "SELECT memory_id,topic_id,weight FROM memory_topic_links"
        ).fetchall()
        for row in linked:
            source, target = f"memory:{row[0]}", f"topic:{row[1]}"
            if source in nodes and target in nodes:
                links.append({"source": source, "target": target, "kind": "topic", "weight": row[2]})

        if scope and scope.startswith("group:"):
            group_clause, group_params = "WHERE gm.group_id=?", [scope.split(":", 1)[1]]
        else:
            group_clause, group_params = "", []
        people = self.db.execute(
            "SELECT gm.group_id,gm.user_id,COALESCE(NULLIF(gm.card,''),gm.nickname,p.nickname,gm.user_id) AS label,"
            "COALESCE(r.affection,0) AS affection FROM group_members gm "
            "LEFT JOIN people p ON p.user_id=gm.user_id LEFT JOIN relations r "
            "ON r.group_id=gm.group_id AND r.user_id=gm.user_id "
            + group_clause + " ORDER BY ABS(COALESCE(r.affection,0)) DESC,gm.updated_at DESC LIMIT ?",
            (*group_params, min(limit, 60)),
        ).fetchall()
        for row in people:
            person_id = f"person:{row[0]}:{row[1]}"
            nodes[person_id] = {
                "id": person_id, "kind": "person", "label": row[2],
                "ref_id": row[1], "group_id": row[0], "weight": abs(int(row[3])) + 1,
            }
            links.append({"source": "bot", "target": person_id, "kind": "relationship", "weight": row[3]})

        # Person nodes are added after memories so connect subject edges now.
        existing = {(link["source"], link["target"], link["kind"]) for link in links}
        for row in self.db.execute(
            "SELECT id,scope,user_id FROM memories WHERE user_id!='_group_' "
            + ("AND scope=? " if scope else "") + "ORDER BY updated_at DESC LIMIT ?",
            (*([scope] if scope else []), limit),
        ).fetchall():
            edge = (f"memory:{row[0]}", f"person:{str(row[1]).split(':', 1)[-1]}:{row[2]}", "subject")
            if edge[0] in nodes and edge[1] in nodes and edge not in existing:
                links.append({"source": edge[0], "target": edge[1], "kind": edge[2]})
        return {"nodes": list(nodes.values()), "links": links}
