"""A hot scope cannot repeatedly consume all background extraction capacity."""

import asyncio

from qqbot.agent.conversation.maintenance import ConversationMaintenance
from qqbot.agent.fairness import ScopeWorkBudget


async def settled(owner):
    async with asyncio.timeout(2):
        while owner.pending_extractions:
            await asyncio.sleep(0)


def test_default_budget_allows_two_extractions_per_scope_then_refuses():
    async def run():
        count = [8]
        calls = []
        async def extract(scope):
            calls.append(scope)
        owner = ConversationMaintenance(message_count=lambda _: count[0], extract_memory=extract, extract_every=8)
        try:
            for _ in range(2):
                assert owner.maybe_extract("group:42")
                await settled(owner)
                count[0] += 8
            assert owner.maybe_extract("group:42") is False
            assert calls == ["group:42", "group:42"]
        finally:
            await owner.aclose()
    asyncio.run(run())


def test_budget_is_independent_between_groups_and_private_and_recovers_after_window():
    async def run():
        now = [0.0]
        async def extract(scope):
            pass
        owner = ConversationMaintenance(message_count=lambda _: 8, extract_memory=extract, extract_every=8)
        owner._extraction_budget = ScopeWorkBudget(max_per_scope=1, window_seconds=10, clock=lambda: now[0])
        try:
            assert owner.maybe_extract("group:7")
            await settled(owner)
            assert owner.maybe_extract("group:7") is False
            assert owner.maybe_extract("private:7")
            assert owner.maybe_extract("group:8")
            await settled(owner)
            now[0] = 10
            assert owner.maybe_extract("group:7")
            await settled(owner)
        finally:
            await owner.aclose()
    asyncio.run(run())


def test_ineligible_or_already_pending_work_does_not_consume_admission_budget():
    async def run():
        count = [0]
        hold = asyncio.Event()
        async def extract(scope):
            await hold.wait()
        owner = ConversationMaintenance(message_count=lambda _: count[0], extract_memory=extract, extract_every=8)
        budget = ScopeWorkBudget(max_per_scope=2)
        owner._extraction_budget = budget
        try:
            assert owner.maybe_extract("group:42") is False
            count[0] = 8
            assert owner.maybe_extract("group:42")
            assert budget.pending("group:42") == 1
            assert owner.maybe_extract("group:42") is False
            assert budget.pending("group:42") == 1
            hold.set()
            await settled(owner)
        finally:
            await owner.aclose()
    asyncio.run(run())


def test_failed_extraction_still_consumes_its_cost_budget():
    async def run():
        async def extract(scope):
            raise RuntimeError("provider failed")
        owner = ConversationMaintenance(message_count=lambda _: 8, extract_memory=extract, extract_every=8)
        owner._extraction_budget = ScopeWorkBudget(max_per_scope=1)
        try:
            assert owner.maybe_extract("group:42")
            await settled(owner)
            assert owner.maybe_extract("group:42") is False
        finally:
            await owner.aclose()
    asyncio.run(run())
