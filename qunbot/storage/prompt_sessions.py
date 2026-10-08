"""Exact model-visible message log for append-only prompt-cache continuity."""

from __future__ import annotations

import json
import time

from .base import SqliteRepository


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS prompt_session_events (
          id INTEGER PRIMARY KEY,
          scope TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          payload_json TEXT NOT NULL,
          created_at INTEGER NOT NULL,
          UNIQUE(scope, sequence)
        );
        CREATE INDEX IF NOT EXISTS prompt_session_scope_sequence
          ON prompt_session_events(scope, sequence);
    """)


class PromptSessionStore(SqliteRepository):
    def load(self, scope: str, limit: int = 1000) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload_json FROM prompt_session_events WHERE scope=? "
            "ORDER BY sequence DESC LIMIT ?", (scope, limit),
        ).fetchall()
        return self._decode(rows)

    def load_recent(
        self, scope: str, *, max_age_seconds: int = 5400, limit: int = 32
    ) -> list[dict]:
        """Read only this live conversation stretch, preserving stored history."""
        cutoff = int(time.time()) - max(1, max_age_seconds)
        rows = self.db.execute(
            "SELECT payload_json FROM prompt_session_events "
            "WHERE scope=? AND created_at>=? ORDER BY sequence DESC LIMIT ?",
            (scope, cutoff, limit),
        ).fetchall()
        return self._decode(rows)

    def _decode(self, rows) -> list[dict]:
        result = []
        for row in reversed(rows):
            try:
                value = json.loads(row[0])
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get("role"):
                result.append(value)
        result = self._without_legacy_operator_turns(result)
        # Older builds persisted the complete dynamic context envelope. Keep
        # those sessions usable after upgrade by retaining only the actual
        # member utterance at the end of the envelope.
        return [self._compact_dynamic_message(message) for message in result]

    @staticmethod
    def _compact_dynamic_message(message: dict) -> dict:
        if message.get("role") != "user":
            return message
        content = message.get("content")
        if not isinstance(content, str) or "当前消息：" not in content:
            return message
        compact = content.rsplit("当前消息：", 1)[-1].strip()
        if compact and len(compact) < len(content):
            return {**message, "content": compact}
        return message

    @staticmethod
    def _without_legacy_operator_turns(messages: list[dict]) -> list[dict]:
        """Hide control prompts written by versions predating event origins.

        Old scheduled turns were persisted as a user message whose dynamic
        context called a bot-authored task "主动群聊", followed by its assistant
        result. Neither belongs in provider replay: the task is control-plane
        input and the actual output already lives in conversation history.
        This read-time compatibility filter avoids deleting healthy cache rows.
        """
        kept: list[dict] = []
        skip_assistant = False
        for message in messages:
            content = message.get("content")
            legacy_operator = (
                message.get("role") == "user"
                and isinstance(content, str)
                and '\"当前场景\": \"主动群聊\"' in content
                and '\"id\": \"bot\"' in content
                and '\"nickname\": \"Bot\"' in content
            )
            if legacy_operator:
                skip_assistant = True
                continue
            if skip_assistant and message.get("role") == "assistant":
                skip_assistant = False
                continue
            # Tool messages may occur between the operator request and result.
            if skip_assistant and message.get("role") == "tool":
                continue
            skip_assistant = False
            kept.append(message)
        return kept

    def append(self, scope: str, messages: list[dict]) -> None:
        if not messages:
            return
        now = int(time.time())
        with self.transaction():
            row = self.db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM prompt_session_events WHERE scope=?",
                (scope,),
            ).fetchone()
            sequence = int(row[0])
            for message in messages:
                sequence += 1
                payload = json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                self.db.execute(
                    "INSERT INTO prompt_session_events(scope,sequence,payload_json,created_at) "
                    "VALUES(?,?,?,?)", (scope, sequence, payload, now),
                )

    def clear(self, scope: str) -> int:
        with self.transaction():
            cursor = self.db.execute("DELETE FROM prompt_session_events WHERE scope=?", (scope,))
        return cursor.rowcount

    def compaction_candidate(
        self, scope: str, *, max_messages: int = 80,
        max_chars: int = 32000, tail_messages: int = 24,
    ) -> dict | None:
        """Return an immutable model input without changing stored history."""
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(MAX(sequence), 0) "
            "FROM prompt_session_events WHERE scope=?",
            (scope,),
        ).fetchone()
        stored_count, sequence = int(row[0]), int(row[1])
        # A compaction commit replaces the entire provider-visible series, so
        # its model input must cover that entire series too. Reading only
        # max_messages + 1 here would silently discard an older prefix when a
        # very busy room accumulated more than one threshold before a reply.
        messages = self.load(scope, limit=max(1, stored_count))
        total_chars = sum(len(str(message.get("content") or "")) for message in messages)
        if len(messages) <= max_messages and total_chars <= max_chars:
            return None
        return {
            "sequence": sequence,
            "head": messages[:-tail_messages],
            "tail": messages[-tail_messages:],
        }

    def replace_compacted(
        self, scope: str, *, expected_sequence: int,
        snapshot: dict, tail: list[dict],
    ) -> bool:
        """Atomically replace history unless another turn arrived meanwhile."""
        with self.transaction():
            row = self.db.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM prompt_session_events WHERE scope=?",
                (scope,),
            ).fetchone()
            if int(row[0]) != expected_sequence:
                return False
            self.db.execute("DELETE FROM prompt_session_events WHERE scope=?", (scope,))
            for sequence, message in enumerate([snapshot, *tail], 1):
                self.db.execute(
                    "INSERT INTO prompt_session_events(scope,sequence,payload_json,created_at) VALUES(?,?,?,?)",
                    (scope, sequence, json.dumps(message, ensure_ascii=False, sort_keys=True, separators=(",", ":")), int(time.time())),
                )
        return True
