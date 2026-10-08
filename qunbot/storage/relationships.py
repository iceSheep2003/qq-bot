"""Persistence for people facts and per-group relationship state.

Two models, deliberately separate:

* ``group_members`` / ``people`` are the *facts* — who someone is, what they
  are called in this particular group, when they were first seen here.
  ``people`` is the global identity index (latest nickname anywhere) and is
  kept only so a group row can fall back to something readable; the per-group
  ``group_members`` row is authoritative for a group.
* ``relations`` / ``affection_events`` are the *interaction state* — the
  bounded score and an append-only audit trail of every proposal, accepted or
  rejected, with its source event and reason.

``RelationshipsStore`` is the only owner of affection data. Nothing else
creates a second score table.

Migration is additive: the original three tables keep their columns and rows,
and new columns are appended only when missing, so an existing database opens
unchanged.
"""

from __future__ import annotations

import time

from ..relationships.state import (
    DAY_SECONDS,
    AffectionProposal,
    RelationshipPolicy,
)
from .base import SqliteRepository

_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # table, column, ddl
    ("affection_events", "source", "source TEXT NOT NULL DEFAULT 'manual'"),
    ("affection_events", "source_event_id", "source_event_id TEXT"),
    ("affection_events", "value_after", "value_after INTEGER"),
    ("affection_events", "status", "status TEXT NOT NULL DEFAULT 'applied'"),
    ("affection_events", "detail", "detail TEXT NOT NULL DEFAULT ''"),
    # When dormancy decay last moved this score. Kept apart from `updated_at`
    # so a decay is never mistaken for an interaction — the cooldown and the
    # daily caps read `affection_events` with status='applied', and a decay
    # writes status='decayed' precisely so it cannot reset them.
    ("relations", "decayed_at", "decayed_at INTEGER NOT NULL DEFAULT 0"),
)


def _add_column(db, table: str, column: str, ddl: str) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def migrate(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS people (
          user_id TEXT PRIMARY KEY, nickname TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS relations (
          group_id TEXT NOT NULL, user_id TEXT NOT NULL,
          affection INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
          PRIMARY KEY(group_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS affection_events (
          id INTEGER PRIMARY KEY, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
          delta INTEGER NOT NULL, reason TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS group_members (
          group_id TEXT NOT NULL, user_id TEXT NOT NULL,
          nickname TEXT NOT NULL DEFAULT '', card TEXT NOT NULL DEFAULT '',
          first_seen_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
          PRIMARY KEY(group_id,user_id)
        );
    """)
    for table, column, ddl in _ADDED_COLUMNS:
        _add_column(db, table, column, ddl)


class RelationshipsStore(SqliteRepository):
    def __init__(self, database, policy: RelationshipPolicy | None = None):
        super().__init__(database)
        self.policy = policy or RelationshipPolicy()

    # ---------------------------------------------------------------- facts

    def observe_user(self, user_id: str, nickname: str) -> None:
        """Global identity index. Signature is part of the PeopleRepository port."""
        self.db.execute(
            "INSERT INTO people(user_id,nickname,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "nickname=excluded.nickname,updated_at=excluded.updated_at",
            (user_id, nickname, int(time.time())),
        )

    def observe_group_member(
        self, group_id: str, user_id: str, nickname: str, card: str = ""
    ) -> None:
        """The per-group fact. A group card is stored for *this* group only.

        Not on ``PeopleRepository`` yet: the conversation service currently
        calls only ``observe_user`` and never passes the group, so wiring this
        requires a port change (reported separately).
        """
        if not group_id or not user_id:
            return
        now = int(time.time())
        with self.transaction():
            self.db.execute(
                "INSERT INTO group_members"
                "(group_id,user_id,nickname,card,first_seen_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET "
                "nickname=excluded.nickname,card=excluded.card,"
                "updated_at=excluded.updated_at",
                (group_id, user_id, nickname or "", card or "", now, now),
            )
            self.observe_user(user_id, nickname or "")

    def members(self, group_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT user_id FROM group_members WHERE group_id=? ORDER BY user_id",
            (group_id,),
        ).fetchall()
        return [self.profile(group_id, row[0]) for row in rows]

    # ------------------------------------------------------- relationship

    def profile(self, group_id: str, user_id: str) -> dict:
        """Readable even when automatic scoring is off: it only reads."""
        person = self.db.execute(
            "SELECT nickname FROM people WHERE user_id=?", (user_id,)
        ).fetchone()
        member = self.db.execute(
            "SELECT nickname, card, first_seen_at FROM group_members "
            "WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        relation = self.db.execute(
            "SELECT affection FROM relations WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        last = self.db.execute(
            "SELECT reason, created_at FROM affection_events "
            "WHERE group_id=? AND user_id=? AND status='applied' "
            "ORDER BY id DESC LIMIT 1",
            (group_id, user_id),
        ).fetchone()
        events = self.db.execute(
            "SELECT count(*) FROM affection_events "
            "WHERE group_id=? AND user_id=? AND status='applied'",
            (group_id, user_id),
        ).fetchone()[0]
        score = relation[0] if relation else 0
        stage = self.policy.stage_for(score)
        nickname = (member[0] if member and member[0] else None) or (
            person[0] if person else ""
        )
        return {
            "user_id": user_id,
            "group_id": group_id,
            "nickname": nickname,
            "card": (member[1] if member else "") or "",
            "affection": score,
            # Stage, not the number, is what the agent should narrate.
            "stage": stage.key,
            "stage_label": stage.label,
            "stage_guidance": stage.guidance,
            "relationship_note": self.policy.narrate(score),
            "last_reason": last[0] if last else "",
            "last_interaction_at": last[1] if last else 0,
            "events": events,
            "first_seen_at": member[2] if member else 0,
        }

    def change_affection(
        self, group_id: str, user_id: str, delta: int, reason: str
    ) -> int:
        """Port write path. Keeps its original signature and ValueError contract."""
        result = self._apply(
            AffectionProposal(group_id, user_id, delta, reason, source="manual")
        )
        if not result["accepted"]:
            raise ValueError(result["rejected"])
        return result["value"]

    def apply_proposal(
        self, proposal: AffectionProposal, *, now: int | None = None
    ) -> dict:
        """The sanctioned write path: a validated proposal plus provenance.

        Returns a decision dict instead of raising, so a rejected proposal can
        still be explained and audited rather than vanishing.
        """
        return self._apply(proposal, now=now)

    def _apply(self, proposal: AffectionProposal, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else int(now)
        proposal = proposal.normalized()
        current = self._score(proposal.group_id, proposal.user_id)
        try:
            proposal.validate(self.policy)
        except ValueError as exc:
            return {"accepted": False, "rejected": str(exc), "value": current}
        with self.transaction():
            if proposal.source_event_id and self._seen(proposal):
                return {"accepted": False, "rejected": "duplicate", "value": current}
            rejected = self._rate_limited(proposal, now)
            if rejected:
                self._log_event(proposal, current, "rejected", rejected, now)
                return {"accepted": False, "rejected": rejected, "value": current}
            value = self.policy.clamp(current + proposal.delta)
            self.db.execute(
                "INSERT INTO relations(group_id,user_id,affection,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(group_id,user_id) DO UPDATE SET "
                "affection=excluded.affection,updated_at=excluded.updated_at",
                (proposal.group_id, proposal.user_id, value, now),
            )
            self._log_event(proposal, value, "applied", "", now)
        stage = self.policy.stage_for(value)
        return {
            "accepted": True,
            "value": value,
            "delta": proposal.delta,
            "score_before": current,
            "reason": proposal.reason,
            "source": proposal.source,
            "source_event_id": proposal.source_event_id,
            "stage": stage.key,
            "stage_label": stage.label,
            "explanation": (
                f"{proposal.source} 事件 {proposal.source_event_id or '(none)'} "
                f"使 {proposal.group_id}/{proposal.user_id} 好感度 "
                f"{current}->{value}：{proposal.reason}"
            ),
        }

    # ---------------------------------------------------------- dormancy

    def decay_dormant(self, *, now: int | None = None) -> list[dict]:
        """Pull relationships nobody has touched back toward neutral.

        Half-life decay measured from the last *applied* interaction, so a
        score only ever moves because time passed, never because this ran more
        often. The clock is ``decayed_at`` rather than ``updated_at``: keeping
        them apart is what stops a decay from looking like an interaction to
        the cooldown and the daily caps.

        Returns only the rows whose *stage* changed — that is the part with
        visible consequences. A score drifting inside a stage changes nothing
        the bot says, so it is applied silently; a stage change is recorded,
        under status ``decayed`` so it never counts as an interaction.
        """
        policy = self.policy
        half_life = int(policy.decay_half_life_seconds)
        if half_life <= 0:
            return []
        now = int(time.time()) if now is None else int(now)
        rows = self.db.execute(
            "SELECT r.group_id, r.user_id, r.affection,"
            " MAX("
            "   COALESCE((SELECT MAX(e.created_at) FROM affection_events e"
            "     WHERE e.group_id=r.group_id AND e.user_id=r.user_id"
            "       AND e.status='applied'), 0),"
            "   r.decayed_at, r.updated_at"
            " ) AS anchor"
            " FROM relations r WHERE r.affection != 0"
            " ORDER BY abs(r.affection) DESC LIMIT ?",
            (max(0, int(policy.decay_max_rows)),),
        ).fetchall()

        stage_changes: list[dict] = []
        for row in rows:
            elapsed = now - int(row["anchor"] or 0) - int(policy.decay_grace_seconds)
            if elapsed <= 0:
                continue
            current = int(row["affection"])
            # Exponentials compose, so stepping this repeatedly reaches the
            # same place as one long step. Rounding decides *when* a step
            # happens, not how far — which is why nothing is written when the
            # score has not moved a whole point yet.
            value = int(round(current * (0.5 ** (elapsed / half_life))))
            if value == current:
                continue
            before = policy.stage_for(current)
            after = policy.stage_for(value)
            with self.transaction():
                self.db.execute(
                    "UPDATE relations SET affection=?, decayed_at=?"
                    " WHERE group_id=? AND user_id=?",
                    (value, now, row["group_id"], row["user_id"]),
                )
                if after.key != before.key:
                    self._log_event(
                        AffectionProposal(
                            row["group_id"],
                            row["user_id"],
                            value - current,
                            "长期未互动，关系阶段回落",
                            source="decay",
                        ),
                        value,
                        "decayed",
                        f"{before.key}->{after.key}",
                        now,
                    )
            if after.key != before.key:
                stage_changes.append(
                    {
                        "group_id": row["group_id"],
                        "user_id": row["user_id"],
                        "before": current,
                        "after": value,
                        "from_stage": before.key,
                        "to_stage": after.key,
                    }
                )
        return stage_changes

    # ---------------------------------------------------------- audit trail

    def history(self, group_id: str, user_id: str, limit: int = 20) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM affection_events WHERE group_id=? AND user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (group_id, user_id, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def explain(self, group_id: str, user_id: str) -> dict:
        rows = self.history(group_id, user_id, 1)
        return rows[0] if rows else {}

    # --------------------------------------------------------------- helpers

    def _score(self, group_id: str, user_id: str) -> int:
        row = self.db.execute(
            "SELECT affection FROM relations WHERE group_id=? AND user_id=?",
            (group_id, user_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def _seen(self, proposal: AffectionProposal) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM affection_events WHERE group_id=? AND user_id=? "
            "AND source_event_id=? LIMIT 1",
            (proposal.group_id, proposal.user_id, proposal.source_event_id),
        ).fetchone()
        return row is not None

    def _rate_limited(self, proposal: AffectionProposal, now: int) -> str | None:
        policy = self.policy
        last = self.db.execute(
            "SELECT created_at FROM affection_events WHERE group_id=? AND user_id=? "
            "AND status='applied' ORDER BY id DESC LIMIT 1",
            (proposal.group_id, proposal.user_id),
        ).fetchone()
        if last and now - last[0] < policy.cooldown_seconds:
            return "affection cooldown active"
        count, positives = self.db.execute(
            "SELECT count(*), coalesce(sum(delta > 0), 0) FROM affection_events "
            "WHERE group_id=? AND user_id=? AND status='applied' AND created_at>=?",
            (proposal.group_id, proposal.user_id, now - DAY_SECONDS),
        ).fetchone()
        if count >= policy.max_events_per_day:
            return "daily event limit reached"
        if proposal.delta > 0 and positives >= policy.max_positive_per_day:
            return "daily positive limit reached"
        return None

    def _log_event(
        self,
        proposal: AffectionProposal,
        value_after: int,
        status: str,
        detail: str,
        now: int,
    ) -> None:
        self.db.execute(
            "INSERT INTO affection_events"
            "(group_id,user_id,delta,reason,created_at,source,source_event_id,"
            "value_after,status,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                proposal.group_id,
                proposal.user_id,
                proposal.delta,
                proposal.reason[:240],
                now,
                proposal.source,
                proposal.source_event_id,
                value_after,
                status,
                detail[:120],
            ),
        )
