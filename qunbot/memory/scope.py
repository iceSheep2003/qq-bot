"""Conversation-scope boundaries for long-term memory.

The same QQ account can appear in many groups, but a person is not a global
memory key.  A memory is addressed by ``(scope, subject)`` and the scope is
resolved from the trusted inbound event, never from model text.  This module
keeps that rule in one small, dependency-free policy so a future storage
adapter cannot accidentally turn a row-level filter into cross-group recall.

Only intentionally public bot behaviour (the static behaviour experience)
may be shared across groups.  Personal facts, relationship state, style
samples, slang, topics and actual dialogue stay in their originating scope.
"""

from __future__ import annotations

import re

from ..ontology.objects import stable_id

_SCOPE = re.compile(r"^(group|private):([A-Za-z0-9_.:-]+)$")


class ScopeViolation(ValueError):
    """An untrusted or mismatched conversation scope was supplied."""


class ConversationScope:
    """Value-level policy for tenant/session isolation."""

    @staticmethod
    def validate(scope: str) -> str:
        value = str(scope or "").strip()
        if not _SCOPE.fullmatch(value) or ".." in value:
            raise ScopeViolation("invalid conversation scope")
        return value

    @classmethod
    def subject_key(cls, scope: str, user_id: str) -> str:
        """Return a person identity that cannot collide between groups."""
        scope = cls.validate(scope)
        user = str(user_id or "").strip()
        if not user:
            raise ScopeViolation("empty subject identity")
        return stable_id("person", scope, user)

    @classmethod
    def permits_recall(cls, requested_scope: str, stored_scope: str) -> bool:
        """Personal/group records are never recalled across scopes."""
        return cls.validate(requested_scope) == cls.validate(stored_scope)


def scoped_person_id(scope: str, user_id: str) -> str:
    """Compatibility function for callers that need a scoped ontology ID."""
    return ConversationScope.subject_key(scope, user_id)
