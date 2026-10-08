"""Human-scale outbound timing, isolated from message composition."""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Awaitable, Callable

from .models import ReplyPlan


@dataclass(frozen=True)
class HumanizedPacer:
    enabled: bool = False
    base_seconds: float = 0.35
    per_character_seconds: float = 0.018
    maximum_seconds: float = 2.4
    followup_seconds: float = 0.65
    random_value: Callable[[], float] = random.random
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    @classmethod
    def from_env(cls) -> "HumanizedPacer":
        def number(name: str, default: float, high: float) -> float:
            try:
                return max(0.0, min(high, float(os.getenv(name, str(default)))))
            except ValueError:
                return default
        return cls(
            enabled=os.getenv("BOT_HUMANIZED_PACING_ENABLED", "true").lower() == "true",
            base_seconds=number("BOT_HUMANIZED_PACING_BASE_SECONDS", .35, 5),
            per_character_seconds=number("BOT_HUMANIZED_PACING_PER_CHAR_SECONDS", .018, .2),
            maximum_seconds=number("BOT_HUMANIZED_PACING_MAX_SECONDS", 2.4, 10),
            followup_seconds=number("BOT_HUMANIZED_PACING_FOLLOWUP_SECONDS", .65, 5),
        )

    async def before(self, plan: ReplyPlan) -> None:
        if not self.enabled:
            return
        length = len(plan.semantic_text)
        delay = self.base_seconds + length * self.per_character_seconds
        delay += self.random_value() * min(.45, delay * .25)
        await self.sleep(min(self.maximum_seconds, delay))

    async def between_parts(self) -> None:
        if self.enabled and self.followup_seconds:
            await self.sleep(self.followup_seconds * (.8 + .4 * self.random_value()))
