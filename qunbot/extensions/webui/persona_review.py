"""Owner console projection for guidance and example history."""

from __future__ import annotations

import json
import time

from ..persona.examples import PersonaExampleManager

STATUSES = ("pending", "accepted", "rejected")


def _json_list(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


class PersonaQueueQuery:
    """Read the queue and record decisions on it."""

    def __init__(self, connection, persona_path=None):
        self.db = connection
        self.examples = PersonaExampleManager(persona_path) if persona_path else None

    def proposals(self, scope: str | None = None, limit: int = 100) -> dict:
        """Pending first, then the decisions already made around them."""
        limit = min(max(1, int(limit)), 200)
        params: list = []
        where = " WHERE 1=1"
        if scope:
            where += " AND scope=?"
            params.append(scope)
        return {
            "pending": self._rows(
                "SELECT * FROM persona_proposals" + where + " AND status='pending'",
                (*params, limit),
            ),
            "decided": self._rows(
                "SELECT * FROM persona_proposals" + where + " AND status!='pending'",
                (*params, limit),
            ),
            "statuses": list(STATUSES),
        }

    def _rows(self, sql: str, params) -> list[dict]:
        rows = self.db.execute(
            sql + " ORDER BY id DESC LIMIT ?", tuple(params)
        ).fetchall()
        return [self._project(row) for row in rows]

    @staticmethod
    def _project(row) -> dict:
        item = dict(row)
        item["evidence"] = _json_list(item.get("evidence"))
        return item

    def decide(
        self, proposal_id: int, status: str, *, note: str = "", now: int | None = None
    ) -> dict | None:
        """Record the deployer's answer. Returns the row, or None if unknown."""
        if status not in ("accepted", "rejected"):
            raise ValueError(f"未知的处置：{status}")
        original = self.db.execute(
            "SELECT * FROM persona_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        if original is None:
            return None
        if original["kind"] == "example" and status == "accepted":
            if self.examples is None:
                raise ValueError("尚未配置人格文件，不能采用示例")
            if original["status"] != "pending":
                raise ValueError("这条示例已经审查过")
            self.examples.apply(dict(original))
        self.db.execute(
            "UPDATE persona_proposals SET status=?, note=?, decided_at=? WHERE id=?",
            (status, str(note)[:200], int(time.time()) if now is None else int(now),
             int(proposal_id)),
        )
        row = self.db.execute(
            "SELECT * FROM persona_proposals WHERE id=?", (int(proposal_id),)
        ).fetchone()
        return self._project(row) if row else None
