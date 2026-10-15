"""Versioned job definitions and run results.

The jobs table predates this module: it grew a ``prompt`` string because every
early action was a chat turn. That does not scale to an action with real
parameters, so a job now carries a versioned :class:`JobSpec` with a small
JSON ``payload`` that the owning action validates itself.

Two rules keep this honest:

* **The spec is an envelope, not a schema.** The scheduler validates the parts
  it must understand (version, schedule kind, payload is a small JSON object)
  and delegates everything else to the action through a validator supplied at
  registration time. The scheduler never learns what "poster" or "chat" mean.
* **Versions are checked, not guessed.** A row written by a newer deployment
  is a startup error naming the job, never a silently misread payload.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Bumped only when the *envelope* changes shape. Adding a key to one action's
#: payload does not bump this; that action validates its own payload.
JOB_SPEC_VERSION = 1

#: Payloads are small on purpose. Anything larger belongs in config or a file
#: the action reads itself, not in a row the scheduler rewrites on every sync.
MAX_PAYLOAD_BYTES = 4096

SCHEDULE_KINDS = frozenset({"at", "every", "cron"})

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_SKIPPED = "skipped"
RUN_FAILED = "failed"
RUN_INTERRUPTED = "interrupted"
RUN_ABANDONED = "abandoned"

#: Statuses a run never leaves. ``finish_job`` only moves a run out of
#: ``running``, so a late writer cannot rewrite a settled outcome.
TERMINAL_STATUSES = frozenset(
    {RUN_SUCCEEDED, RUN_SKIPPED, RUN_FAILED, RUN_INTERRUPTED, RUN_ABANDONED}
)


class JobTimeout(Exception):
    """A handler outlived the scheduler's timeout.

    Distinct from a handler's own failure so the run detail says "timed out"
    rather than blaming whatever the handler was doing. Recorded as ``failed``
    — a stuck handler is a bug, not a decision to stay silent.
    """


def encode_payload(payload: Mapping[str, Any] | None) -> str:
    """Canonical JSON for storage, so a no-op resync compares equal."""
    return json.dumps(
        dict(payload or {}), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def check_payload(payload: Any, *, key: str = "") -> dict:
    """Return ``payload`` as a plain dict, or raise for an unusable one."""
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError(
            f"job {key!r} payload must be an object, got {type(payload).__name__}"
        )
    materialized = dict(payload)
    try:
        encoded = encode_payload(materialized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"job {key!r} payload must be JSON-serializable: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"job {key!r} payload exceeds {MAX_PAYLOAD_BYTES} bytes; "
            "put large parameters in config instead"
        )
    return materialized


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read one column from a sqlite3.Row or a plain dict."""
    try:
        value = row[name]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


@dataclass(frozen=True)
class JobSpec:
    """What a job *is*, independent of any single run.

    ``key`` is the stable operator-facing identity (``config_key``); ``db_id``
    is the row id handlers see as ``job["id"]``.
    """

    action: str
    kind: str
    value: str
    prompt: str = ""
    group_id: str = ""
    key: str = ""
    db_id: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    version: int = JOB_SPEC_VERSION

    @classmethod
    def from_row(cls, row: Any) -> "JobSpec":
        """Build a spec from a jobs row, rejecting an unknown version."""
        version = int(_field(row, "spec_version", JOB_SPEC_VERSION))
        key = str(_field(row, "config_key", "") or "")
        if version != JOB_SPEC_VERSION:
            raise ValueError(
                f"job {key!r} was written by a newer scheduler "
                f"(spec version {version}, this build understands "
                f"{JOB_SPEC_VERSION}). Upgrade the bot or remove the job."
            )
        payload = _field(row, "payload", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            action=str(_field(row, "action", "chat")),
            kind=str(_field(row, "schedule_kind", "")),
            value=str(_field(row, "schedule_value", "")),
            prompt=str(_field(row, "prompt", "") or ""),
            group_id=str(_field(row, "group_id", "") or ""),
            key=key,
            db_id=_field(row, "id"),
            payload=check_payload(payload, key=key),
            version=version,
        )

    def validate(self) -> None:
        """Envelope checks that hold for every action."""
        if self.kind not in SCHEDULE_KINDS:
            raise ValueError(f"job {self.key!r} has unsupported schedule {self.kind!r}")
        if not self.action:
            raise ValueError(f"job {self.key!r} needs an action")
        if not 0 <= len(self.prompt) <= 500:
            raise ValueError(f"job {self.key!r} prompt must be 0-500 characters")
        check_payload(self.payload, key=self.key)


@dataclass(frozen=True)
class JobRunResult:
    """The settled outcome of one reserved run.

    A run row is created ``running`` by ``reserve_job`` and only ever leaves
    that status once (see :data:`TERMINAL_STATUSES`). The lease columns are how
    a *different* process can tell a live run from an abandoned one.
    """

    run_id: int
    status: str
    detail: str = ""
    started_at: int | None = None
    finished_at: int | None = None
    lease_owner: str | None = None
    lease_expires_at: int | None = None
    attempt: int = 1

    @property
    def settled(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @classmethod
    def from_row(cls, row: Any) -> "JobRunResult":
        return cls(
            run_id=int(_field(row, "id", 0)),
            status=str(_field(row, "status", "")),
            detail=str(_field(row, "detail", "") or ""),
            started_at=_field(row, "started_at"),
            finished_at=_field(row, "finished_at"),
            lease_owner=_field(row, "lease_owner"),
            lease_expires_at=_field(row, "lease_expires_at"),
            attempt=int(_field(row, "attempt", 1)),
        )
