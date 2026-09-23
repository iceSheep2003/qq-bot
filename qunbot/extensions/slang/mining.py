"""Candidate discovery: frequent chunks minus the general vocabulary.

Deliberately model-free. "This group keeps saying something the language at
large does not" is a counting question, and a counting question answered
locally is auditable, offline-testable and free. A model's guess about what a
phrase means would be another untrusted derived value on top of untrusted
group text, with no way for a deployer to check it.

The output is a *candidate with evidence*: how often, by how many people, over
how many days, and in which sentences. Nothing here decides to use anything.
"""

from __future__ import annotations

import re
from pathlib import Path

# What a term is allowed to look like once it reaches the prompt. This is the
# last line of defence, not a nicety: a term is group text, and group text can
# contain newlines, quotes and role markers. Anything outside this shape is
# dropped rather than escaped, so no candidate can introduce structure.
TERM_RE = re.compile(r"^[0-9A-Za-z_一-鿿]+$")

_CQ = re.compile(r"\[CQ:[^\]]*\]")
_URL = re.compile(r"https?://\S+|www\.\S+")
_AT = re.compile(r"@\S+")
_CJK_RUN = re.compile(r"[一-鿿]+")
_ALNUM_RUN = re.compile(r"[0-9A-Za-z_]+")
_LONG_NUMBER = re.compile(r"^\d{4,}$")

_DEFAULT_WORDS = Path(__file__).resolve().parent / "data" / "common_words.txt"

# Evidence targets behind ``confidence``. Six uses by three people across three
# days is a group's habit; anything near that is worth a deployer's attention.
TARGET_OCCURRENCES = 6.0
TARGET_USERS = 3.0
TARGET_DAYS = 3.0


def load_wordlist(*paths: Path | None) -> frozenset[str]:
    """Every term in ``data/common_words.txt`` plus any deployer-supplied file."""
    words: set[str] = set()
    for path in (_DEFAULT_WORDS, *paths):
        if path is None:
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            words.update(line.split())
    return frozenset(words)


def normalize(text: str, *, max_chars: int = 200) -> str:
    """Strip platform markup; drop messages too long to be chat.

    A pasted essay or a link dump is not a group's habitual phrasing, and
    counting n-grams over it would drown the real signal.
    """
    if not isinstance(text, str):
        return ""
    if len(text) > max_chars:
        return ""
    text = _CQ.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _AT.sub(" ", text)
    return text


def is_noise(term: str, words: frozenset[str], *, min_chars: int, max_chars: int) -> bool:
    if not term or not TERM_RE.match(term):
        return True
    if not min_chars <= len(term) <= max_chars:
        return True
    if term in words:
        return True
    # Four-plus digits are a number (a QQ号, a page count, a year), not a word.
    return bool(_LONG_NUMBER.match(term))


def candidates_from(
    text: str, *, min_chars: int = 2, max_chars: int = 12, max_ngram: int = 4
) -> list[str]:
    """Every plausible chunk in one message, in order, duplicates kept.

    Duplicates are the point: a phrase repeated inside one message is repeated
    evidence. CJK runs yield character n-grams because Chinese is not
    whitespace-delimited; latin runs yield whole word tokens.
    """
    chunks: list[str] = []
    for run in _CJK_RUN.findall(text):
        upper = min(max_ngram, len(run))
        for size in range(min_chars, upper + 1):
            for start in range(len(run) - size + 1):
                chunks.append(run[start : start + size])
    for token in _ALNUM_RUN.findall(text):
        lowered = token.lower()
        if len(lowered) >= min_chars:
            chunks.append(lowered)
    return [chunk for chunk in chunks if len(chunk) <= max_chars]


def sanitize(term: str, *, max_chars: int = 12) -> str:
    """``term`` if it is safe to place in a prompt, else the empty string."""
    if not isinstance(term, str):
        return ""
    term = term.strip()
    if not term or len(term) > max_chars or not TERM_RE.match(term):
        return ""
    return term


def confidence(
    occurrences: int,
    users: int,
    days: int,
    *,
    target_occurrences: float = TARGET_OCCURRENCES,
    target_users: float = TARGET_USERS,
    target_days: float = TARGET_DAYS,
) -> float:
    """0..1 evidence score. Frequency first, breadth second, persistence last."""
    def scale(value: float, target: float) -> float:
        if target <= 0:
            return 0.0
        return min(1.0, max(0.0, float(value) / target))

    score = (
        0.5 * scale(occurrences, target_occurrences)
        + 0.3 * scale(users, target_users)
        + 0.2 * scale(days, target_days)
    )
    return round(min(1.0, max(0.0, score)), 4)
