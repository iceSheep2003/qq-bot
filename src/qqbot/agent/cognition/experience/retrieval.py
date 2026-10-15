"""Read-only retrieval from the published behavior snapshot.

The snapshot is intentionally small at runtime.  Retrieval is lexical and
deterministic for now; a future local embedding index may implement the same
port without changing the returned BehaviorOption contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


_WORD = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_HINTS = {
    "answer_directly": "问题 询问 求助 怎么 资料 答案 推荐",
    "acknowledge": "分享 进度 观点 感叹 附和 理解",
    "agree": "同意 确实 对 共识 认同",
    "empathize": "难受 累 崩 懊恼 焦虑 失望",
    "banter": "玩笑 调侃 夸张 梗 幽默 接话",
    "tease_gently": "调侃 逗 反差 轻松",
    "supplement": "补充 解释 资料 方法 经验 信息",
    "clarify": "疑问 区别 定义 具体 澄清 怎么理解",
    "encourage": "进步 加油 坚持 鼓励 成绩 好消息",
    "deescalate": "争论 误解 冲突 生气 缓和",
    "topic_shift": "转移 冷场 换话题 接不上",
    "celebrate": "好消息 恭喜 成功 分享 提升",
    "abstain": "不知道 不确定 隐私 复制粘贴 不参与",
}


@dataclass(frozen=True)
class BehaviorOption:
    strategy: str
    dialogue_act: str
    expression_pattern: str
    avoid: tuple[str, ...]
    media_intent: str
    support_case_count: int
    examples: tuple[dict, ...]
    score: float
    snapshot_version: str


def _terms(value: str) -> set[str]:
    chars = [item for item in _WORD.findall(value.lower()) if item.strip()]
    result = {item for item in chars if len(item) > 1}
    chinese = "".join(item for item in chars if "\u4e00" <= item <= "\u9fff")
    for size in (2, 3):
        result.update(chinese[index : index + size] for index in range(len(chinese) - size + 1))
    return result


class BehaviorRetriever:
    """Load a bounded candidate snapshot and return ranked behavior options."""

    def __init__(self, snapshot: Path | str):
        self.snapshot = Path(snapshot)
        manifest_path = self.snapshot / "manifest.json"
        options_path = self.snapshot / "published_options.jsonl"
        if not manifest_path.is_file() or not options_path.is_file():
            raise ValueError("behavior snapshot is incomplete")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with options_path.open(encoding="utf-8") as stream:
            self.options = [json.loads(line) for line in stream if line.strip()]
        self._hints = {strategy: _terms(text) for strategy, text in _HINTS.items()}

    def retrieve(self, query: str, *, limit: int = 3) -> list[BehaviorOption]:
        if not isinstance(query, str) or not query.strip():
            return []
        limit = max(1, min(3, int(limit)))
        query_terms = _terms(query)
        ranked = []
        for option in self.options:
            strategy = option["strategy"]
            examples = option.get("representatives", [])
            corpus = " ".join(
                [strategy, *self._hints.get(strategy, ())]
                + [
                    " ".join(
                        str(example.get(field, ""))
                        for field in ("situation", "dialogue_act", "expression_pattern", "avoid", "media_intent")
                    )
                    for example in examples
                ]
            )
            corpus_terms = _terms(corpus)
            overlap = query_terms & corpus_terms
            score = float(len(overlap)) + min(2.0, option.get("support_case_count", 0) / 1000.0)
            if score <= 0:
                continue
            ranked.append(
                (
                    score,
                    option.get("support_case_count", 0),
                    BehaviorOption(
                        strategy=strategy,
                        dialogue_act=examples[0].get("dialogue_act", "wait") if examples else "wait",
                        expression_pattern=examples[0].get("expression_pattern", "") if examples else "",
                        avoid=tuple(examples[0].get("avoid", [])) if examples else (),
                        media_intent=examples[0].get("media_intent", "none") if examples else "none",
                        support_case_count=int(option.get("support_case_count", 0)),
                        examples=tuple(examples[:2]),
                        score=score,
                        snapshot_version=str(self.manifest.get("pipeline_version", "unknown")),
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2].strategy))
        return [item[2] for item in ranked[:limit]]
