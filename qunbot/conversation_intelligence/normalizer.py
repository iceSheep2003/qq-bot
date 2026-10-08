from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import ToneSignal

_ASCII = re.compile(r"[0-9a-z_+#.-]{2,}")
_CJK = re.compile(r"[\u3400-\u9fff]+")
_ENTITY = re.compile(r"#[^\s#]{1,24}|[0-9]{4}|[A-Za-z][A-Za-z0-9_+#.-]{1,31}")
_STOP = frozenset({"这个", "那个", "然后", "就是", "可以", "还是", "一下", "什么", "怎么", "哈哈"})
_POSITIVE = ("好耶", "谢谢", "开心", "厉害", "喜欢", "赞", "哈哈", "笑死")
_NEGATIVE = ("烦", "难受", "生气", "讨厌", "离谱", "无语", "滚", "傻")
_HOSTILE = ("滚", "闭嘴", "傻逼", "废物", "去死")


@dataclass(frozen=True)
class Features:
    tokens: frozenset[str]
    entities: frozenset[str]
    tone: ToneSignal


def analyze(text: str) -> Features:
    normalized = " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())
    tokens: set[str] = set(_ASCII.findall(normalized))
    for run in _CJK.findall(normalized):
        if run not in _STOP:
            tokens.update(run[i:i + 2] for i in range(max(0, len(run) - 1)))
    tokens = {token for token in tokens if token not in _STOP and token.strip()}
    entities = {item.casefold() for item in _ENTITY.findall(normalized)}
    pos = sum(normalized.count(word) for word in _POSITIVE)
    neg = sum(normalized.count(word) for word in _NEGATIVE)
    punctuation = min(1.0, (normalized.count("!") + normalized.count("！") + normalized.count("?你") + normalized.count("？？")) / 3)
    polarity = max(-1.0, min(1.0, (pos - neg) / max(1, pos + neg)))
    return Features(
        frozenset(sorted(tokens)[:80]),
        frozenset(sorted(entities)[:24]),
        ToneSignal(
            polarity=polarity,
            intensity=max(punctuation, min(1.0, (pos + neg) / 3)),
            question=any(mark in normalized for mark in ("?", "？", "吗", "呢")),
            joking=any(word in normalized for word in ("哈哈", "笑死", "绷不住", "doge")),
            hostile=any(word in normalized for word in _HOSTILE),
        ),
    )


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
