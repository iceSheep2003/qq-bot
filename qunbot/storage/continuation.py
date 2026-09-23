"""Persistent state for the "X minutes after the last message" workflow.

The conversation table is already the clock: the newest human message in a
scope defines when the group fell quiet. Copying that timestamp here would
create a second source of truth that can drift, so it is derived instead.

What *is* stored here is the part a restart used to lose — the decisions
layered on top of the quiet window:

``window_closed_at``   the current quiet window has already been answered (or
                       abandoned), so the bot waits for the group to speak
                       again instead of repeating itself.
``last_attempt_at``    when this window was last evaluated, so a tick cannot
                       ask the model twice in a row.
``silent_streak``      consecutive "nothing worth saying" decisions inside one
                       window; it lengthens the wait between attempts.

Two tables, both group-scoped. Neither records a ``user_id``: the privacy
deletion sweep keys on that column and this state is about the room, not a
person in it.
"""

from __future__ import annotations

import time

from .base import SqliteRepository

# How many decisions per group are kept for inspection. The table is an audit
# trail, not a log file; older rows are pruned on write.
DECISION_HISTORY = 200

DEFAULT_STATE = {
    "window_closed_at": None,
    "last_attempt_at": 0,
    "silent_streak": 0,
    "last_outcome": "",
}


def migrate(db) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS continuation_state (
          scope TEXT PRIMARY KEY,
          group_id TEXT NOT NULL,
          window_closed_at INTEGER,
          last_attempt_at INTEGER NOT NULL DEFAULT 0,
          silent_streak INTEGER NOT NULL DEFAULT 0,
          last_outcome TEXT NOT NULL DEFAULT '',
          updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS continuation_decisions (
          id INTEGER PRIMARY KEY,
          group_id TEXT NOT NULL,
          scope TEXT NOT NULL,
          decided_at INTEGER NOT NULL,
          outcome TEXT NOT NULL,
          reason TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS continuation_decisions_group
          ON continuation_decisions(group_id, decided_at DESC);
        """
    )


class ContinuationStore(SqliteRepository):
    """Read and write the per-group continuation window.

    Constructed from anything exposing ``.db`` and ``.transaction`` — the
    shared :class:`SqliteDatabase` or any repository built on it — so an
    extension that already holds a conversation repository needs no new wiring
    to reach these tables.
    """

    def state(self, scope: str) -> dict:
        """The window state, or the defaults. Reading never writes."""
        row = self.db.execute(
            "SELECT * FROM continuation_state WHERE scope=?", (scope,)
        ).fetchone()
        return dict(row) if row is not None else dict(DEFAULT_STATE)

    def close_window(self, scope: str, group_id: str, now: int, outcome: str) -> None:
        """End the current quiet window until the group speaks again."""
        with self.transaction():
            self.db.execute(
                "INSERT INTO continuation_state"
                "(scope,group_id,window_closed_at,last_attempt_at,silent_streak,last_outcome,updated_at)"
                " VALUES(?,?,?,?,0,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET window_closed_at=excluded.window_closed_at,"
                " last_attempt_at=excluded.last_attempt_at, silent_streak=0,"
                " last_outcome=excluded.last_outcome, updated_at=excluded.updated_at",
                (scope, group_id, now, now, outcome, now),
            )
            self._record(group_id, scope, now, outcome, "")

    def note_attempt(self, scope: str, group_id: str, now: int, reason: str) -> None:
        """Record an evaluated attempt that produced no post.

        ``silent_streak`` only grows here: a window that was never evaluated
        (the group is still talking) must not lengthen the next wait.
        """
        with self.transaction():
            self.db.execute(
                "INSERT INTO continuation_state"
                "(scope,group_id,window_closed_at,last_attempt_at,silent_streak,last_outcome,updated_at)"
                " VALUES(?,?,NULL,?,1,?,?)"
                " ON CONFLICT(scope) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,"
                " silent_streak=continuation_state.silent_streak+1,"
                " last_outcome=excluded.last_outcome, updated_at=excluded.updated_at",
                (scope, group_id, now, "silent", now),
            )
            self._record(group_id, scope, now, "silent", reason)

    def note_observed(self, group_id: str, scope: str, now: int, reason: str) -> None:
        """Record a decision that did not even open the quiet window."""
        self._record(group_id, scope, now, "observed", reason)

    def decisions(self, group_id: str, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM continuation_decisions WHERE group_id=?"
            " ORDER BY id DESC LIMIT ?",
            (group_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def forget_scope(self, scope: str) -> int:
        with self.transaction():
            removed = self.db.execute(
                "DELETE FROM continuation_state WHERE scope=?", (scope,)
            ).rowcount or 0
            removed += self.db.execute(
                "DELETE FROM continuation_decisions WHERE scope=?", (scope,)
            ).rowcount or 0
        return removed

    def _record(
        self, group_id: str, scope: str, now: int, outcome: str, reason: str
    ) -> None:
        # The table is an audit trail of what changed, not a tick log: a group
        # that has been "waiting for the group to speak again" for an hour is
        # one fact, recorded once. Consecutive repeats are folded away.
        last = self.db.execute(
            "SELECT outcome, reason FROM continuation_decisions WHERE group_id=?"
            " ORDER BY id DESC LIMIT 1",
            (group_id,),
        ).fetchone()
        if last is not None and last["outcome"] == outcome and last["reason"] == reason[:300]:
            return
        self.db.execute(
            "INSERT INTO continuation_decisions(group_id,scope,decided_at,outcome,reason)"
            " VALUES(?,?,?,?,?)",
            (group_id, scope, now, outcome, reason[:300]),
        )
        self.db.execute(
            "DELETE FROM continuation_decisions WHERE group_id=? AND id NOT IN"
            " (SELECT id FROM continuation_decisions WHERE group_id=?"
            "  ORDER BY id DESC LIMIT ?)",
            (group_id, group_id, DECISION_HISTORY),
        )

    @staticmethod
    def now() -> int:
        return int(time.time())
