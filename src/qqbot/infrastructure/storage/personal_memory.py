"""Read-only public facts and persistent personal continuity settings."""

import time

from qqbot.infrastructure.storage.base import SqliteRepository


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS personal_continuity_settings (
        user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        updated_at INTEGER NOT NULL
    )""")


class PersonalMemoryStore(SqliteRepository):

    def public_preferences(self, user_id, scopes, limit=3):
        scopes = sorted(scope for scope in scopes if scope.startswith("group:"))
        if not scopes or limit <= 0:
            return []
        placeholders = ",".join("?" for _ in scopes)
        with self.transaction():
            if not self.enabled(user_id):
                return []
            rows = self.db.execute(
                f"""SELECT m.id, m.content, m.scope, m.source_event_id, m.updated_at
                FROM memories m JOIN messages s ON s.event_id=m.source_event_id
                  AND s.scope=m.scope AND s.user_id=m.user_id AND s.role='user'
                JOIN message_metadata d ON d.event_id=s.event_id
                WHERE m.user_id=? AND m.origin_user_id=m.user_id
                  AND m.scope IN ({placeholders}) AND m.status='active'
                  AND m.fact_type='preference' AND m.visibility='group'
                  AND m.confidence>=0.8 AND m.mention_policy IN ('direct','soft_echo')
                  AND d.origin='member' AND d.delivery_status='received'
                  AND (m.expires_at IS NULL OR m.expires_at>?)
                ORDER BY m.updated_at DESC,m.id DESC LIMIT ?""",
                (user_id, *scopes, int(time.time()), min(3, int(limit))),
            ).fetchall()
        return [
            {"memory_id": row["id"], "content": row["content"],
             "source_scope": row["scope"], "source_event_id": row["source_event_id"],
             "updated_at": row["updated_at"]}
            for row in rows
        ]

    def enabled(self, user_id):
        row = self.db.execute(
            "SELECT enabled FROM personal_continuity_settings WHERE user_id=?", (user_id,)
        ).fetchone()
        return row is None or bool(row["enabled"])

    def set_enabled(self, user_id, enabled):
        with self.transaction():
            self.db.execute("""INSERT INTO personal_continuity_settings VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
                (user_id, int(bool(enabled)), int(time.time())),
            )
