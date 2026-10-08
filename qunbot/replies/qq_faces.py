"""Safe, semantic selection of QQ's built-in face segments.

The model never supplies a numeric face id.  IDs are deployer-owned data and
the local selector maps ordinary reply text to a small, conservative set that
has existed in QQ/OneBot for years.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Callable


DEFAULT_QQ_FACES: dict[str, tuple[str, tuple[str, ...]]] = {
    "微笑": ("14", ("你好", "可以", "好呀", "没问题", "早上好")),
    "呲牙": ("13", ("哈哈", "笑死", "好耶", "乐", "开心")),
    "流泪": ("5", ("难过", "呜呜", "哭", "遗憾", "可惜")),
    "害羞": ("6", ("谢谢", "夸", "喜欢", "不好意思")),
    "睡": ("8", ("晚安", "困了", "睡觉", "熬不动")),
    "发怒": ("11", ("生气", "气死", "离谱", "过分")),
    "赞": ("76", ("厉害", "不错", "支持", "牛", "赞", "恭喜")),
    "胜利": ("78", ("成功", "搞定", "完成", "赢了", "通过")),
    # QQ built-in animated stickers ("超级表情"). NapCat expands these face
    # IDs with AniStickerPackId/AniStickerId from its own face_config.json.
    "超级赞": ("364", ("太强了", "绝了", "狠狠点赞", "太厉害")),
    "超级OK": ("398", ("稳了", "妥了", "完全可以", "就这么办")),
    "祝贺": ("370", ("上岸", "恭喜你", "庆祝", "好消息")),
    "超级鼓掌": ("375", ("鼓掌", "掌声", "牛哇", "漂亮")),
    "狗狗笑哭": ("361", ("绷不住", "笑不活", "太好笑了", "哈哈哈哈")),
    "快乐": ("400", ("快乐", "高兴", "开心死了", "起飞")),
    "真棒": ("380", ("真棒", "做得好", "有东西", "优秀")),
    "冒泡": ("371", ("冒个泡", "有人吗", "出来聊天", "水群")),
    "企鹅疑问": ("367", ("啥情况", "为什么啊", "真的假的", "不理解")),
    "太头疼": ("388", ("头疼", "麻了", "好难", "看不懂")),
    "太气了": ("385", ("太气了", "气人", "受不了", "离大谱")),
    "超级晚安": ("384", ("睡啦", "先睡了", "早点休息", "做个好梦")),
    "送你花花": ("409", ("送你花", "辛苦了", "安慰一下", "奖励你")),
    "我听听": ("407", ("展开说说", "细说", "我听着", "然后呢")),
    "路过": ("381", ("路过", "围观", "看看", "吃瓜")),
}


def _probability(name: str, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv(name, str(default)))))
    except ValueError:
        return default


@dataclass(frozen=True)
class QQFacePolicy:
    probability: float = 0.18
    faces: dict[str, tuple[str, tuple[str, ...]]] | None = None
    random_value: Callable[[], float] = random.random

    @classmethod
    def from_env(cls) -> "QQFacePolicy":
        return cls(probability=_probability("BOT_MEDIA_QQ_FACE_PROBABILITY", 0.18))

    def choose(self, text: str) -> tuple[str, str] | None:
        if not text or self.random_value() >= self.probability:
            return None
        catalog = self.faces or DEFAULT_QQ_FACES
        lowered = text.lower()
        matches = [
            (name, face_id)
            for name, (face_id, keywords) in catalog.items()
            if any(keyword.lower() in lowered for keyword in keywords)
        ]
        return random.choice(matches) if matches else None
