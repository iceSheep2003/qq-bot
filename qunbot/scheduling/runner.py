"""Dispatch scheduled actions using a narrow conversation capability surface."""

from __future__ import annotations

from datetime import date

from ..domain import JobSkipped, MessageEvent
from .registry import DEFAULT_ACTION, JobHandlerRegistry
from .spec import JobSpec


class JobRunner:
    def __init__(self, conversation, handlers: JobHandlerRegistry):
        self.conversation = conversation
        self.handlers = handlers
        self.policy = conversation.policy
        self.agent = conversation.agent
        self.conversations = conversation.conversations
        self.activity = conversation.activity
        self.sender = conversation.sender
        self.last_reply = conversation.last_reply

    def scope_lock(self, scope: str):
        return self.conversation.scope_lock(scope)

    async def send_reply(self, group_id: str | None, user_id: str | None, text: str) -> str:
        return await self.conversation.send_reply(group_id, user_id, text)

    def today_start(self) -> int:
        return self.conversation.today_start()

    def local_today(self) -> date:
        return self.conversation.local_today()

    def job_spec(self, job: dict) -> JobSpec:
        """The typed view of a job row, payload included.

        A handler with parameters reads them from ``job_spec(job).payload``
        rather than string-matching them out of ``job["prompt"]``.
        """
        return JobSpec.from_row(job)

    def job_event(self, job: dict, now: int) -> MessageEvent:
        group_id = job["group_id"]
        return MessageEvent(
            f"job:{job['id']}:{job['run_id']}", f"group:{group_id}", group_id,
            "bot", "Bot", job["prompt"], (), False, (), now,
        )

    async def run(self, job: dict) -> None:
        if job["group_id"] not in self.policy.allowed_groups:
            raise JobSkipped("group is not allowlisted")
        if not self.conversation.within_active_hours():
            raise JobSkipped("outside active hours")
        await self.handlers.get(job.get("action") or DEFAULT_ACTION).run(self, job)
