"""The two reply policies.

A policy answers one question — "does this turn warrant a reply at all?" — and
must be able to say no. Abstaining is a decision with a reason, not a failure:
the message stays in the transcript and simply goes unanswered.

Both policies expose the same shape:

``decide(event, recent=...)``   the async port; returns ``bool``
``evaluate(event, recent)``     the synchronous decision, with score and reason
``last``                        the most recent ``Decision``, for logging or replay

``evaluate`` is the real implementation and ``decide`` is a thin async wrapper
over it. Splitting them this way keeps the heuristic pure and replayable while
still honouring the port, which is async because a future implementation may
call a model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain import MessageEvent
from .config import ReplyPolicyConfig

# Bare acknowledgements, laughter and mood particles. A group says these to each
# other constantly; answering each one is how a bot becomes wallpaper.
FILLERS = frozenset(
    {
        "哈", "哈哈", "哈哈哈", "哈哈哈哈", "呵", "呵呵", "嘿嘿", "嗯", "嗯嗯", "哦",
        "哦哦", "噢", "啊", "啊啊", "呃", "额", "emmm", "emm", "。。。", "...", "…",
        "草", "好", "好的", "收到", "ok", "okk", "OK", "行", "可", "可以", "冲",
        "1", "6", "666", "？？", "？？？", "?", "？", "在吗", "打卡", "晚安",
        "早安", "睡了", "撤了", "笑死", "真实", "谢谢", "谢谢！", "谢谢啦",
        "感谢", "辛苦了", "没事", "没事的",
    }
)

_QUESTION_MARKS = ("？", "?")
_QUESTION_WORDS = (
    "吗", "呢", "怎么", "怎样", "如何", "为什么", "为啥", "什么", "多少", "哪",
    "谁", "能不能", "可不可以", "是不是", "有没有", "对不对", "值不值",
)


@dataclass(frozen=True)
class Decision:
    """A verdict plus the reasoning that produced it.

    ``reason`` is written to be loggable verbatim: an abstention that cannot be
    explained cannot be tuned.
    """

    reply: bool
    reason: str
    score: float = 0.0
    signals: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # convenient in tests and replay loops
        return self.reply

    def as_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "reason": self.reason,
            "score": round(self.score, 3),
            "signals": list(self.signals),
        }


def _history(event: MessageEvent, recent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The transcript *before* this turn.

    ``ConversationService`` records the inbound message before it decides, so the
    message under evaluation is usually the last row of ``recent``. It is dropped
    by ``event_id`` (never by position) so a policy can judge the message in the
    context that actually preceded it. Malformed rows are skipped rather than
    raising: a broken transcript must not break the decision.
    """
    rows: list[dict[str, Any]] = []
    for row in recent or ():
        if not isinstance(row, dict):
            continue
        if row.get("event_id") == event.event_id:
            continue
        rows.append(row)
    return rows


def _is_bot(row: dict[str, Any]) -> bool:
    return row.get("role") == "assistant"


def _created(row: dict[str, Any]) -> int:
    try:
        return int(row.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0


def _is_question(text: str) -> bool:
    if any(mark in text for mark in _QUESTION_MARKS):
        return True
    return any(word in text for word in _QUESTION_WORDS)


class MentionOnlyPolicy:
    """The built-in rule as an object: group messages must @ the bot.

    Private messages are always answered, matching the default in
    ``ConversationService.decide_reply`` exactly — switching to this mode is a
    no-op by construction, which is what makes it the "back to the old
    behaviour" lever.
    """

    name = "mention_only"

    def __init__(self, config: ReplyPolicyConfig | None = None):
        self.config = config or ReplyPolicyConfig()
        self.last: Decision | None = None

    def evaluate(
        self, event: MessageEvent, recent: list[dict[str, Any]] | None = None
    ) -> Decision:
        if not event.group_id:
            return Decision(True, "私聊：直接回复")
        if event.at_bot:
            return Decision(True, "被 @ 了")
        return Decision(False, "群聊且没有被 @：默认沉默")

    async def decide(
        self, event: MessageEvent, *, recent: list[dict[str, Any]]
    ) -> bool:
        self.last = self.evaluate(event, recent)
        return self.last.reply


class RoomReadingPolicy:
    """Deterministic read-the-room heuristic.

    Signals are additive and every one of them is a fact about the transcript or
    the @ state — no model, no randomness, no clock of its own (time comes from
    ``event.timestamp``), so a replay of the same sequence always produces the
    same decisions. The bot speaks when the total reaches ``threshold``.

    The bias is deliberate: abstention is cheap (a missed quip), a false reply is
    expensive (the bot barging in). Signals that mean "this is not for me" are
    weighted heavier than signals that mean "this might be for me".
    """

    name = "room"

    def __init__(self, config: ReplyPolicyConfig | None = None):
        self.config = config or ReplyPolicyConfig()
        self.last: Decision | None = None

    # --- decision ---------------------------------------------------------

    def evaluate(
        self, event: MessageEvent, recent: list[dict[str, Any]] | None = None
    ) -> Decision:
        # Direct address is never a judgement call.
        if not event.group_id:
            return Decision(True, "私聊：直接回复", 1.0)
        if event.at_bot:
            return Decision(True, "被 @ 了", 1.0)

        rows = _history(event, recent)
        text = (event.text or "").strip()
        now = int(event.timestamp or 0)

        named = self._named(text)
        if named:
            return Decision(True, f"正文点了 Bot 的名字（{named}）", 1.0)

        score = 0.0
        signals: list[str] = []

        def add(delta: float, signal: str) -> None:
            nonlocal score
            score += delta
            signals.append(f"{signal}{delta:+.2f}")

        # Addressed to someone who is not the bot.
        if event.at_users:
            add(-0.9, "叫的是别人")

        last_bot = self._last_bot_at(rows)
        continues_bot_turn = bool(rows) and _is_bot(rows[-1])
        gap: int | None = now - last_bot if last_bot is not None else None
        engaged = gap is not None and 0 <= gap < self.config.engaged_window_seconds
        if gap is not None and 0 <= gap:
            if gap < self.config.cooldown_seconds and not continues_bot_turn:
                # The bot just spoke and this is not a reply to it: let the room
                # have its own turn.
                add(-0.5, "刚回过话")
            elif engaged:
                add(0.3, "刚参与过这段对话")

        # A meaningful message immediately after the bot's turn is ordinarily
        # a continuation even without an @ or question mark. Chinese group
        # chat often omits punctuation ("那你是bot还是人类"), so requiring an
        # explicit question signal cuts a live conversation in half.
        if continues_bot_turn and text and not self._is_filler(text) and not event.at_users:
            add(0.45, "直接接着 Bot 的上一句")

        if _is_question(text):
            add(0.45, "有人在提问")
            if continues_bot_turn:
                # The message immediately answers/extends the bot's own turn.
                add(0.4, "接着 Bot 的话在问")
            if engaged and not self._human_after(rows, last_bot):
                # An open question nobody picked up, on a thread the bot is
                # still part of. Deliberately gated on ``engaged``: twenty
                # minutes later the room has moved on, and crediting the bot
                # with an unanswered question it no longer owns is exactly how
                # a policy starts barging in.
                add(0.25, "问题还没人接")

        if self._is_filler(text):
            add(-0.6, "寒暄或语气词")
        if event.image_urls and not text:
            add(-0.8, "只有图片")

        if self._is_burst(rows, now):
            add(-0.4, "刷屏")
        if self._is_crowd(rows):
            add(-0.2, "人多")

        return self._verdict(score, signals)

    def _verdict(self, score: float, signals: list[str]) -> Decision:
        reply = score >= self.config.threshold
        verdict = "开口" if reply else "沉默"
        detail = "；".join(signals) if signals else "没有任何值得接话的信号"
        reason = (
            f"{verdict}（score={score:.2f}，threshold="
            f"{self.config.threshold:.2f}）：{detail}"
        )
        return Decision(reply, reason, score, tuple(signals))

    async def decide(
        self, event: MessageEvent, *, recent: list[dict[str, Any]]
    ) -> bool:
        self.last = self.evaluate(event, recent)
        return self.last.reply

    # --- signals ----------------------------------------------------------

    def _named(self, text: str) -> str:
        for name in self.config.bot_names:
            if name and name in text:
                return name
        return ""

    @staticmethod
    def _last_bot_at(rows: list[dict[str, Any]]) -> int | None:
        for row in reversed(rows):
            if _is_bot(row):
                return _created(row)
        return None

    @staticmethod
    def _human_after(rows: list[dict[str, Any]], since: int) -> bool:
        return any(
            not _is_bot(row) and _created(row) > since for row in rows
        )

    def _is_burst(self, rows: list[dict[str, Any]], now: int) -> bool:
        window = [
            row
            for row in rows
            if 0 <= now - _created(row) <= self.config.burst_seconds
        ]
        # +1 for the message under evaluation: a room where five things landed in
        # twenty seconds is talking to itself.
        return len(window) + 1 >= self.config.burst_count

    def _is_crowd(self, rows: list[dict[str, Any]]) -> bool:
        speakers = {
            row.get("user_id") for row in rows[-8:] if not _is_bot(row)
        }
        speakers.discard(None)
        speakers.discard("")
        return len(speakers) >= self.config.crowd_speakers

    @staticmethod
    def _is_filler(text: str) -> bool:
        if not text:
            return True
        if text in FILLERS:
            return True
        # Two characters or fewer with no question mark is a nod, not a turn.
        return len(text) <= 2 and not any(m in text for m in _QUESTION_MARKS)
