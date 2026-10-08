"""Durable bounded source events for rebuilding the derived topic index."""

from __future__ import annotations

import json

from ..domain import MessageEvent
from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS topic_observations (
          event_id TEXT PRIMARY KEY, scope TEXT NOT NULL, payload_json TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS topic_observations_scope_time
          ON topic_observations(scope, created_at DESC);
    """)


class TopicObservationStore(SqliteRepository):
    def add(self, event: MessageEvent) -> None:
        payload = {
            "event_id": event.event_id, "scope": event.scope,
            "group_id": event.group_id, "user_id": event.user_id,
            "nickname": event.nickname, "text": event.text,
            "image_urls": event.image_urls, "at_bot": event.at_bot,
            "at_users": event.at_users, "timestamp": event.timestamp,
            "card": event.card, "platform_message_id": event.platform_message_id,
            "reply_to_message_id": event.reply_to_message_id,
            "reply_to_user_id": event.reply_to_user_id,
            "quoted_text": event.quoted_text,
            "quoted_image_urls": event.quoted_image_urls,
            "origin": event.origin,
        }
        self.db.execute(
            "INSERT OR IGNORE INTO topic_observations(event_id,scope,payload_json,created_at) VALUES(?,?,?,?)",
            (event.event_id, event.scope, json.dumps(payload, ensure_ascii=False), event.timestamp),
        )

    def recent(self, scope: str, limit: int = 200) -> list[MessageEvent]:
        rows = self.db.execute(
            "SELECT payload_json FROM topic_observations WHERE scope=? ORDER BY created_at DESC,rowid DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        result = []
        for row in reversed(rows):
            try:
                payload = json.loads(row[0])
                payload["image_urls"] = tuple(payload.get("image_urls") or ())
                payload["at_users"] = tuple(payload.get("at_users") or ())
                payload["quoted_image_urls"] = tuple(payload.get("quoted_image_urls") or ())
                result.append(MessageEvent(**payload))
            except (TypeError, ValueError):
                continue
        return result
