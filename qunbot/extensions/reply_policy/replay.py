"""Offline replay: score a reply policy against a labelled message sequence.

No gateway, no model, no real group. A ``ReplayCase`` is a short transcript plus
the message that arrived at the end of it, and a human label saying whether a
good bot would have spoken. Running a policy over a corpus of them produces the
two numbers that decide whether the policy is safe to turn on:

``false_reply_rate``  the bot spoke where the room wanted silence. This is the
                      expensive error: it is what makes a bot disruptive.
``miss_rate``         the bot stayed silent where it was expected to answer.
                      This is the cheap error: the conversation simply goes on.

The rates are ratios over *labelled opportunities*, not over all cases: the
false-reply rate divides by the number of should-stay-silent turns, so a corpus
with mostly-silent turns cannot be gamed into a good score by abstaining on
everything.

Feed it a real transcript by building ``ReplayCase`` objects from recorded
messages; nothing here talks to NapCat and nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from ...domain import MessageEvent

# Fixed decision point so a replay is reproducible to the second.
BASE_TIME = 1_700_000_000


class _Policy(Protocol):
    async def decide(
        self, event: MessageEvent, *, recent: list[dict[str, Any]]
    ) -> bool: ...


@dataclass(frozen=True)
class ReplayMessage:
    """One message in a recorded sequence.

    ``ago`` is seconds before the decision point, so a case reads as a
    conversation rather than a table of epoch values. ``content`` is the text as
    the transcript stores it (an image-only frame is stored as ``[图片]`` while
    the event itself carries no text — ``image=True`` reproduces that).
    """

    role: str  # "user" | "assistant"
    user_id: str
    nickname: str
    content: str
    ago: int = 0
    at_bot: bool = False
    at_users: tuple[str, ...] = ()
    image: bool = False

    @property
    def created_at(self) -> int:
        return BASE_TIME - self.ago

    def row(self, event_id: str) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "scope": "group:10001",
            "user_id": self.user_id,
            "nickname": self.nickname,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ReplayCase:
    """A transcript, the message under test, and the label it should get."""

    name: str
    history: tuple[ReplayMessage, ...]
    incoming: ReplayMessage
    should_reply: bool
    note: str = ""


def to_event(case: ReplayCase) -> MessageEvent:
    incoming = case.incoming
    return MessageEvent(
        event_id=f"replay:{case.name}",
        scope="group:10001",
        group_id="10001",
        user_id=incoming.user_id,
        nickname=incoming.nickname,
        # An image-only frame carries no text on the event, exactly as
        # ``parse_message`` builds it.
        text="" if incoming.image else incoming.content,
        image_urls=("http://example.invalid/pic.jpg",) if incoming.image else (),
        at_bot=incoming.at_bot,
        at_users=incoming.at_users,
        timestamp=BASE_TIME,
    )


def to_recent(case: ReplayCase) -> list[dict[str, Any]]:
    """The transcript as ``ConversationService`` hands it to a policy.

    The inbound message is recorded *before* the decision, so it is the last row
    here too. A policy that forgets this will read its own message as history.
    """
    event = to_event(case)
    rows = [message.row(f"h{i}") for i, message in enumerate(case.history)]
    rows.append(case.incoming.row(event.event_id))
    return rows


@dataclass
class CaseOutcome:
    name: str
    expected: bool
    decided: bool
    reason: str
    score: float = 0.0

    @property
    def correct(self) -> bool:
        return self.expected == self.decided

    @property
    def false_reply(self) -> bool:
        return self.decided and not self.expected

    @property
    def missed_reply(self) -> bool:
        return self.expected and not self.decided


@dataclass
class ReplayReport:
    policy: str
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def silent_cases(self) -> int:
        return sum(1 for o in self.outcomes if not o.expected)

    @property
    def reply_cases(self) -> int:
        return sum(1 for o in self.outcomes if o.expected)

    @property
    def false_replies(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.false_reply]

    @property
    def missed_replies(self) -> list[CaseOutcome]:
        return [o for o in self.outcomes if o.missed_reply]

    @property
    def false_reply_rate(self) -> float:
        """Spoke when it should have stayed quiet, over the quiet turns."""
        if not self.silent_cases:
            return 0.0
        return len(self.false_replies) / self.silent_cases

    @property
    def miss_rate(self) -> float:
        """Stayed quiet when it should have answered, over the answerable turns."""
        if not self.reply_cases:
            return 0.0
        return len(self.missed_replies) / self.reply_cases

    @property
    def accuracy(self) -> float:
        if not self.outcomes:
            return 1.0
        return sum(1 for o in self.outcomes if o.correct) / len(self.outcomes)

    def summary(self) -> str:
        return (
            f"{self.policy}: {self.total} turns, "
            f"false_reply={len(self.false_replies)}/{self.silent_cases} "
            f"({self.false_reply_rate:.1%}), "
            f"missed={len(self.missed_replies)}/{self.reply_cases} "
            f"({self.miss_rate:.1%}), accuracy={self.accuracy:.1%}"
        )


async def replay_case(policy: _Policy, case: ReplayCase) -> CaseOutcome:
    decided = bool(await policy.decide(to_event(case), recent=to_recent(case)))
    last = getattr(policy, "last", None)
    return CaseOutcome(
        name=case.name,
        expected=case.should_reply,
        decided=decided,
        reason=getattr(last, "reason", ""),
        score=getattr(last, "score", 0.0),
    )


async def replay(policy: _Policy, cases: Iterable[ReplayCase]) -> ReplayReport:
    """Run the policy over every case on one event loop."""
    report = ReplayReport(policy=getattr(policy, "name", type(policy).__name__))
    for case in cases:
        report.outcomes.append(await replay_case(policy, case))
    return report


def run_replay(policy: _Policy, cases: Iterable[ReplayCase]) -> ReplayReport:
    """Blocking wrapper around :func:`replay`, for tools and unit tests."""
    return asyncio.run(replay(policy, list(cases)))


def compare(
    policies: Iterable[_Policy], cases: Iterable[ReplayCase]
) -> list[ReplayReport]:
    """Score several policies on the same corpus so the trade is visible."""
    corpus = list(cases)
    return [run_replay(policy, corpus) for policy in policies]
