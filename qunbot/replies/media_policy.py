"""Local, configurable nudges for casual media replies.

The model still chooses the words. This policy only changes their channel or
adds a catalogue image when the local context makes that reasonable.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Callable


def _probability(name: str, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


_TAG_HINTS = {
    "暧昧": ("心动", "撩你", "想你", "脸红了"),
    "疑惑": ("什么意思", "不懂", "没看懂", "这也行", "为啥"),
    "震惊": ("真的假的", "离谱", "震惊", "啊？", "不是吧"),
    "无语": ("无语", "服了", "没话说", "又来", "沉默"),
    "崩溃": ("绷不住", "麻了", "裂开", "救命"),
    "害羞": ("害羞", "脸红", "别夸", "不好意思"),
    "困了": ("困了", "睡了", "晚安", "熬不动"),
    "吃瓜": ("吃瓜", "看戏", "有瓜", "什么故事"),
    "调侃": ("笨蛋", "你呀", "逗你", "想得美"),
    "开心": ("开心", "好耶", "乐了", "高兴"),
    "生气": ("气死", "生气", "太过分", "气人"),
    "烧脑": ("烧脑", "脑子转不动", "cpu", "算晕了"),
    "犯傻": ("犯傻", "我傻了", "笨住", "看笑话"),
    "要钱": ("打钱", "请客", "红包", "转账"),
    "喜欢": ("喜欢", "好喜欢", "有点爱"),
    "早安": ("早安", "早上好", "早呀"),
    "等回复": ("等你说", "我等着", "快说"),
    "难过": ("难过", "委屈", "想哭", "伤心"),
    "工作": ("上班", "工作", "加班", "打工"),
    "加油": ("加油", "冲呀", "稳住", "可以的"),
    "观望": ("围观", "吃瓜", "看看", "细说"),
    "拒绝": ("不要", "不行", "打住", "休想"),
    "思考": ("我想想", "琢磨", "等等", "有点意思"),
    "打call": ("好耶", "太棒", "厉害", "牛哇", "恭喜"),
}


@dataclass(frozen=True)
class CasualMediaPolicy:
    voice_probability: float = 0.20
    followup_meme_probability: float = 0.40
    max_voice_chars: int = 65
    random_value: Callable[[], float] = random.random
    random_meme_probability: float = 0.20

    @classmethod
    def from_env(cls) -> "CasualMediaPolicy":
        return cls(
            voice_probability=_probability("BOT_MEDIA_CASUAL_VOICE_PROBABILITY", .20),
            followup_meme_probability=_probability("BOT_MEDIA_FOLLOWUP_MEME_PROBABILITY", .40),
            random_meme_probability=_probability("BOT_MEDIA_RANDOM_MEME_PROBABILITY", 0.0),
        )

    def voice_suitable(self, text: str) -> bool:
        if not text or len(text) > self.max_voice_chars:
            return False
        # Precise information should remain copyable; serious distress should
        # not be converted to a chirpy voice by a random roll.
        return not self.serious_or_precise(text)

    def serious_or_precise(self, text: str) -> bool:
        return any(word in text for word in (
            "http", "分数线", "招生人数", "报录比", "命令", "代码", "公式",
            "自杀", "轻生", "活不下去", "报警", "急救",
        ))

    def followup_tag(self, text: str, available: frozenset[str]) -> str:
        for tag, hints in _TAG_HINTS.items():
            if tag in available and any(hint in text for hint in hints):
                return tag
        return ""

    def random_tag(self, text: str, available: frozenset[str]) -> str:
        if not text or self.serious_or_precise(text):
            return ""
        casual = sorted(available.intersection({
            "疑惑", "震惊", "无语", "观望", "吃瓜", "犯傻", "开心",
            "卖萌", "害羞", "调侃", "烧脑", "思考",
        }))
        if not casual:
            return ""
        index = min(int(self.random_value() * len(casual)), len(casual) - 1)
        return casual[index]
