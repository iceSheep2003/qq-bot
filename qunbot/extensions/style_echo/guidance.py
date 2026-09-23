"""Turn raw samples into a *restricted style description* — never an identity.

The anti-impersonation guarantee is structural, not a promise in a docstring:

1. ``analyze`` returns a :class:`StyleProfile` whose fields are counts, ratios
   and booleans. Raw sample text is never carried into the profile, so there is
   no field through which a name, a user ID or a sentence could travel.
2. ``render`` takes **only** a profile. It has no access to a nickname, a
   user_id or the samples themselves, so it *cannot* name anyone even if it
   wanted to.
3. The output vocabulary is a closed set of fixed clauses. The only variable
   parts are booleans selecting a clause and a couple of particles drawn from a
   fixed tuple — never free text copied from a message.
4. ``assert_safe`` runs on the rendered string and raises if any impersonation
   marker or a digit appears. It is fail-closed: a bug that let hostile text
   through would raise rather than ship an instruction to the model.

The result reads as "prefers short sentences; ends with ellipses", which is a
description of a writing habit. It never reads as "you are X" or "speak as X".
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A render is refused if it contains any of these. Kept as a tuple so a test can
# assert the guard is installed and non-empty.
FORBIDDEN_MARKERS: tuple[str, ...] = (
    "扮演",
    "冒充",
    "假装",
    "模仿",
    "模拟",
    "你是",
    "本人",
    "身份",
    "自称",
    "代替",
    "取代",
)

# Fixed clause vocabulary. Every rendered sentence comes from here.
_LENGTH_CLAUSES: tuple[tuple[str, str], ...] = (
    ("short", "偏好短句"),
    ("medium", "句子长短适中"),
    ("long", "常用信息量较大的长句"),
)
_ELLIPSIS = "句末常用省略号"
_QUESTION = "常用疑问语气收尾"
_EXCLAIM = "情绪外露，常用感叹号"
_TILDE = "语气偏软，常用波浪号"
_LAUGHTER = "常用「哈哈」这类笑声词"
_EMOJI = "偶尔使用 emoji"
_PARTICLES = "句尾常用「{}」这类语气词"

_PARTICLE_SET = ("呀", "呢", "吧", "嘛", "啦", "哦", "啊", "咯")
_LAUGHTER_MARKERS = ("哈哈", "嘿嘿", "嘻嘻", "🤣", "😂")
_TRAILING = "！？。～~…!?. \t\n"
_MIN_EMOJI = 0x1F300
_MAX_EMOJI = 0x1FAFF
_KAOMOJI_LOW, _KAOMOJI_HIGH = 0x2600, 0x27BF

# The one-line framing every guidance string carries. Deliberately contains no
# impersonation marker and no user-supplied text.
GUIDANCE_PREFIX = "表达风格参考（仅句式统计，不指代任何人）："


class UnsafeGuidance(ValueError):
    """Raised when a rendered guidance string would violate the style contract."""


@dataclass(frozen=True)
class StyleProfile:
    """Aggregate features of a sample set. No text, no identity, no raw values."""

    samples: int
    avg_chars: float
    length: str
    ellipsis: bool = False
    question: bool = False
    exclaim: bool = False
    tilde: bool = False
    laughter: bool = False
    emoji: bool = False
    particles: tuple[str, ...] = field(default_factory=tuple)


def _has_emoji(text: str) -> bool:
    return any(
        _MIN_EMOJI <= ord(ch) <= _MAX_EMOJI or _KAOMOJI_LOW <= ord(ch) <= _KAOMOJI_HIGH
        for ch in text
    )


def _has_tilde(text: str) -> bool:
    return "~" in text or "～" in text


def _length_bucket(avg: float) -> str:
    if avg < 12:
        return "short"
    if avg > 40:
        return "long"
    return "medium"


def analyze(samples: list[str], *, min_samples: int = 5) -> StyleProfile | None:
    """Distil samples into aggregate features, or ``None`` when there is too little.

    Fewer than ``min_samples`` messages is not a habit, and a bot that claims a
    style on the strength of two lines is both wrong and creepy. Note that a
    sample may contain any text at all — a name, an order, an injection — and
    none of it survives this function.
    """
    texts = [s.strip() for s in samples if s and s.strip()]
    if len(texts) < min_samples:
        return None

    particles: dict[str, int] = {}
    ellipsis = question = exclaim = tilde = laughter = emoji = 0
    total_chars = 0
    for text in texts:
        total_chars += len(text)
        stripped = text.rstrip(_TRAILING)
        for particle in _PARTICLE_SET:
            if stripped.endswith(particle):
                particles[particle] = particles.get(particle, 0) + 1
                break
        if "…" in text or "......" in text or "。。。" in text:
            ellipsis += 1
        if text.rstrip().endswith(("?", "？")) or "吗" in text or "呢" in text:
            question += 1
        if text.rstrip().endswith(("!", "！", "！~")):
            exclaim += 1
        if _has_tilde(text):
            tilde += 1
        if any(marker in text for marker in _LAUGHTER_MARKERS):
            laughter += 1
        if _has_emoji(text):
            emoji += 1

    total = len(texts)
    # A trait is reported only when it recurs: one exclamation mark is noise,
    # a third of the messages ending in "！" is a habit.
    threshold = max(2, total // 3)
    habits = tuple(
        particle
        for particle, count in sorted(particles.items(), key=lambda kv: -kv[1])
        if count >= threshold
    )[:2]
    avg = total_chars / total
    return StyleProfile(
        samples=total,
        avg_chars=round(avg, 1),
        length=_length_bucket(avg),
        ellipsis=ellipsis >= threshold,
        question=question >= threshold,
        exclaim=exclaim >= threshold,
        tilde=tilde >= threshold,
        laughter=laughter >= threshold,
        emoji=emoji >= threshold,
        particles=habits,
    )


def assert_safe(text: str) -> str:
    """Fail-closed guard. Returns ``text`` unchanged when it is safe."""
    for marker in FORBIDDEN_MARKERS:
        if marker in text:
            raise UnsafeGuidance(f"guidance contains an impersonation marker: {marker}")
    if any(ch.isdigit() for ch in text):
        raise UnsafeGuidance("guidance contains digits, which could carry an ID")
    return text


def render(profile: StyleProfile) -> str:
    """A bounded, identity-free style note. Accepts no names, by signature."""
    clauses = [dict(_LENGTH_CLAUSES)[profile.length]]
    if profile.ellipsis:
        clauses.append(_ELLIPSIS)
    if profile.tilde:
        clauses.append(_TILDE)
    if profile.exclaim:
        clauses.append(_EXCLAIM)
    if profile.question:
        clauses.append(_QUESTION)
    if profile.laughter:
        clauses.append(_LAUGHTER)
    if profile.particles:
        clauses.append(_PARTICLES.format("」「".join(profile.particles)))
    if profile.emoji:
        clauses.append(_EMOJI)
    body = "；".join(clauses)
    return assert_safe(f"{GUIDANCE_PREFIX}{body}。")
