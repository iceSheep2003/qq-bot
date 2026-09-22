"""Small text helpers shared by dedupe and ranking.

Chinese has no whitespace word boundaries, so a character n-gram model is used
instead of whitespace tokens. Pure functions only.
"""

from __future__ import annotations

_IGNORED = set(
    " \t\r\n　!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    "，。！？、；：（）《》【】…—～·“”‘’"
)


def normalize(text: str) -> str:
    return "".join(ch for ch in text.strip().lower() if ch not in _IGNORED)


def char_ngrams(text: str, size: int = 2) -> set[str]:
    """Character n-grams. Falls back to the whole string when very short."""
    cleaned = normalize(text)
    if not cleaned:
        return set()
    if len(cleaned) <= size:
        return {cleaned}
    return {cleaned[start : start + size] for start in range(len(cleaned) - size + 1)}


def similarity(left: str, right: str, size: int = 2) -> float:
    """Jaccard overlap of character n-grams, 0..1."""
    first, second = char_ngrams(left, size), char_ngrams(right, size)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def query_tokens(query: str) -> set[str]:
    """Terms a user message is matched against: whitespace words for latin
    text, character n-grams for CJK runs."""
    tokens: set[str] = set()
    for word in query.replace('"', " ").split():
        if len(word) > 1:
            tokens.add(word.lower())
        tokens |= char_ngrams(word)
    return tokens
