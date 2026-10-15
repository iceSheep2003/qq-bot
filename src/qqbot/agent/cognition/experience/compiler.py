"""Compile graph retrieval results into a small model-facing context.

The compiler is deterministic and has no model or platform dependency.  It
keeps the graph as evidence-backed experience while preventing an entire graph
or repeated examples from entering the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from qqbot.agent.cognition.experience.retrieval import BehaviorOption


@dataclass(frozen=True)
class CompiledBehavior:
    strategy: str
    dialogue_act: str
    expression_pattern: str
    avoid: tuple[str, ...]
    media_intent: str
    support_case_count: int
    example_cases: tuple[dict, ...]
    score: float


@dataclass(frozen=True)
class BehaviorContext:
    """Bounded behavior input for PromptAssembler."""

    snapshot_version: str
    options: tuple[CompiledBehavior, ...]
    conflicts: tuple[str, ...]
    omitted_options: int
    char_budget: int

    def as_dict(self) -> dict:
        return {
            "snapshot_version": self.snapshot_version,
            "options": [
                {
                    "strategy": option.strategy,
                    "dialogue_act": option.dialogue_act,
                    "expression_pattern": option.expression_pattern,
                    "avoid": list(option.avoid),
                    "media_intent": option.media_intent,
                    "support_case_count": option.support_case_count,
                    "examples": list(option.example_cases),
                    "score": round(option.score, 4),
                }
                for option in self.options
            ],
            "conflicts": list(self.conflicts),
            "omitted_options": self.omitted_options,
        }

    def render(self) -> str:
        """Render a stable, bounded text block for a model prompt."""
        if not self.options:
            return "行为经验：本轮没有命中可用候选。"
        lines = ["行为经验候选（仅供参考，不是固定台词）："]
        for index, option in enumerate(self.options, 1):
            lines.append(f"{index}. 策略：{option.strategy}；动作：{option.dialogue_act}")
            lines.append(f"   表达结构：{option.expression_pattern}")
            if option.avoid:
                lines.append("   避免：" + "；".join(option.avoid[:4]))
            if option.media_intent != "none":
                lines.append(f"   媒体倾向：{option.media_intent}")
            if option.example_cases:
                lines.append("   案例：" + option.example_cases[0].get("situation", ""))
        if self.conflicts:
            lines.append("注意：候选存在以下不确定性：" + "；".join(self.conflicts))
        text = "\n".join(lines)
        # Keep the prompt budget deterministic.  Prefer complete lines over a
        # character-level cut that can produce a misleading half-instruction.
        if len(text) <= self.char_budget:
            return text
        kept = []
        size = 0
        for line in lines:
            extra = len(line) + (1 if kept else 0)
            if size + extra > self.char_budget:
                break
            kept.append(line)
            size += extra
        return "\n".join(kept)


class BehaviorCompiler:
    def __init__(self, *, max_options: int = 3, max_cases: int = 2, char_budget: int = 1200):
        if not 1 <= max_options <= 8:
            raise ValueError("max_options must be between 1 and 8")
        if not 0 <= max_cases <= 8:
            raise ValueError("max_cases must be between 0 and 8")
        if not 240 <= char_budget <= 8000:
            raise ValueError("char_budget must be between 240 and 8000")
        self.max_options = max_options
        self.max_cases = max_cases
        self.char_budget = char_budget

    def compile(self, options: list[BehaviorOption]) -> BehaviorContext:
        if not options:
            return BehaviorContext("none", (), (), 0, self.char_budget)
        ordered = sorted(options, key=lambda item: (-item.score, -item.support_case_count, item.strategy))
        merged: dict[str, CompiledBehavior] = {}
        conflicts: list[str] = []
        for option in ordered:
            current = merged.get(option.strategy)
            if current is None:
                merged[option.strategy] = CompiledBehavior(
                    strategy=option.strategy,
                    dialogue_act=option.dialogue_act,
                    expression_pattern=option.expression_pattern,
                    avoid=tuple(dict.fromkeys(option.avoid))[:6],
                    media_intent=option.media_intent,
                    support_case_count=option.support_case_count,
                    example_cases=tuple(option.examples[: self.max_cases]),
                    score=option.score,
                )
                continue
            if current.dialogue_act != option.dialogue_act:
                conflicts.append(f"{option.strategy} 的对话动作存在多个候选")
            if current.media_intent != option.media_intent:
                conflicts.append(f"{option.strategy} 的媒体倾向存在多个候选")
        selected = tuple(list(merged.values())[: self.max_options])
        return BehaviorContext(
            snapshot_version=ordered[0].snapshot_version,
            options=selected,
            conflicts=tuple(dict.fromkeys(conflicts))[:6],
            omitted_options=max(0, len(merged) - len(selected)),
            char_budget=self.char_budget,
        )
