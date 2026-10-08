"""Scheduled style learning; immutable persona core, bounded example window.

In auto mode only an actual delivered exchange may enter the managed example
window. General model-generated guidance remains a separate manual-review mode.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable
from difflib import SequenceMatcher
from pathlib import Path

from ...content import is_media_placeholder
from ...storage.persona_review import persona_digest
from .config import PersonaConfig
from .examples import PersonaExampleManager
from .proposals import PersonaProposer, ProposalError
from .selection import ExampleSelector

log = logging.getLogger(__name__)

#: What the model sees of the group's chatter. Enough to read a room, small
#: enough that a pass stays cheap.
MAX_TRANSCRIPT_CHARS = 4000
_MESSAGE_CHARS = 200
MAX_EXAMPLES_PER_SCAN = 2
_UNSAFE_SAMPLE = re.compile(
    r"忽略|忘掉|系统提示|提示词|人设|设定|开发者|管理员|运行命令|执行命令|"
    r"api.?key|token|密码|密钥|[0-9]{6,}|@(?:全体成员|all)", re.I
)
_FACTUAL_ASSERTION = re.compile(
    r"确实|一定|肯定|官方|分数线|招生|报录比|之前是|原来是|已经是|就是(?:数据|模型|学校)"
)


def _scene_label(user_text: str) -> str:
    """Closed-vocabulary cue: raw member text never enters the stable persona."""
    if any(word in user_text for word in ("数学", "做题", "刷题", "真题")):
        return "群友：吐槽做题不顺"
    if any(word in user_text for word in ("熬夜", "失眠", "困了", "睡不着")):
        return "群友：聊到最近没睡好"
    if any(word in user_text for word in ("复习", "进度", "学习")):
        return "群友：吐槽复习状态"
    return "群友：随口聊起今天的事"


def _sample_text(value: object, *, maximum: int) -> str:
    text = " ".join(str(value or "").split())
    if not 4 <= len(text) <= maximum:
        return ""
    if (
        text == "+1" or text.startswith(("/", "!", "["))
        or is_media_placeholder(text) or "http://" in text or "https://" in text
        or any(mark in text for mark in ("<", ">", "{", "}", "`"))
        or re.search(r"[A-Za-z0-9]", text)
        or _UNSAFE_SAMPLE.search(text)
    ):
        return ""
    return text


def example_candidates(rows: list[dict], *, after_id: int = 0) -> list[dict]:
    """Queue actual short user→bot exchanges, never model-invented dialogue."""
    examples: list[dict] = []
    for previous, current in reversed(list(zip(rows, rows[1:]))):
        if previous.get("role") != "user" or current.get("role") != "assistant":
            continue
        if int(current.get("id") or 0) <= after_id:
            continue
        user_text = _sample_text(previous.get("content"), maximum=80)
        bot_text = _sample_text(current.get("content"), maximum=90)
        if not user_text or not bot_text:
            continue
        if (bot_text.endswith(("?", "？")) or len(bot_text) > 35
                or _FACTUAL_ASSERTION.search(bot_text)):
            continue
        if SequenceMatcher(None, user_text, bot_text).ratio() >= 0.78:
            continue
        left, right = int(previous.get("created_at") or 0), int(current.get("created_at") or 0)
        if left and right and not 0 <= right - left <= 300:
            continue
        examples.append({
            "kind": "example",
            "suggestion": f"{_scene_label(user_text)}\nKinna：{bot_text}",
            "rationale": "Bot 回复确已发送；群友原话仅作筛选证据，不写进人设",
            "evidence": [{
                "user_event_id": str(previous.get("event_id") or ""),
                "assistant_event_id": str(current.get("event_id") or ""),
            }],
        })
        if len(examples) >= MAX_EXAMPLES_PER_SCAN:
            break
    return examples


def _last_id(rows: list[dict]) -> int:
    return max((int(row.get("id") or 0) for row in rows), default=0)


class PersonaProposalQueue:
    def __init__(
        self,
        store,
        config: PersonaConfig,
        proposer: PersonaProposer,
        scopes: Iterable[str],
        reader: Callable[[str, int], list[dict]],
        persona_source: Callable[[], str],
        *,
        persona_path: Path | None = None,
        model=None,
        incremental_reader: Callable[[str, int, int], list[dict]] | None = None,
    ):
        self.store = store
        self.config = config
        self.proposer = proposer
        self.scopes = tuple(scopes)
        self.reader = reader
        # A callable, not a string: a deployer who edits the file between two
        # passes should get suggestions against what the file says now, not
        # against whatever it said at startup.
        self.persona_source = persona_source
        self.example_manager = PersonaExampleManager(persona_path) if persona_path else None
        self.example_selector = ExampleSelector(model) if model else None
        self.incremental_reader = incremental_reader

    def persona(self) -> str:
        try:
            return str(self.persona_source() or "")
        except OSError:
            log.warning("persona file is unreadable; skipping this pass")
            return ""

    def render(self, rows: list[dict]) -> str:
        lines = []
        for row in rows:
            text = str(row.get("content") or "").strip()
            if not text:
                continue
            speaker = str(row.get("nickname") or row.get("user_id") or "群友")
            lines.append(f"{speaker}: {text[:_MESSAGE_CHARS]}")
        return "\n".join(lines)[:MAX_TRANSCRIPT_CHARS]

    def fresh(self, scope: str, last_id: int) -> list[dict]:
        try:
            if self.incremental_reader is not None:
                rows = self.incremental_reader(scope, last_id, 500) or []
            else:
                rows = self.reader(scope, self.config.proposal_window_messages) or []
        except Exception:
            log.exception("persona proposal could not read %s", scope)
            return []
        return [
            row
            for row in rows
            if int(row.get("id") or 0) > last_id and row.get("role") == "user"
        ]

    async def propose_for(self, scope: str, *, now: int | None = None) -> int:
        """One pass over one scope. Returns how many suggestions were queued.

        A pass with nothing to say is the common case, and it is cheap: an
        interval that has not elapsed, or too few new messages, returns before
        the model is ever asked.
        """
        now = int(time.time()) if now is None else int(now)
        last_id, last_run = self.store.last_scan(scope)
        if now - last_run < self.config.proposal_interval_hours * 3600:
            return 0
        rows = self.fresh(scope, last_id)
        if len(rows) < self.config.proposal_min_messages:
            return 0
        if not self.persona():
            return 0

        try:
            scene_rows = self.reader(scope, self.config.proposal_window_messages) or []
        except Exception:
            log.exception("persona examples could not read %s", scope)
            scene_rows = []
        examples = example_candidates(scene_rows, after_id=last_id)
        if self.config.auto_examples_enabled:
            added = await self._evolve(scope, examples, scene_rows, len(rows), now)
        else:
            try:
                suggestions = await self.proposer.propose(self.persona(), self.render(rows))
            except ProposalError:
                log.warning("persona proposal reply was unusable for %s", scope)
                suggestions = []
            except Exception:
                log.exception("persona proposal pass failed for %s", scope)
                suggestions = []
            added = self.store.record(
                scope, [*suggestions, *examples],
                persona_hash=persona_digest(self.persona()),
                batch_size=len(rows), now=now,
            )
        self.store.advance_scan(scope, _last_id(rows))
        self.store.mark_run(scope, now=now)
        if added:
            log.info("persona proposals scope=%s queued=%s", scope, added)
        return added

    async def _evolve(self, scope: str, examples: list[dict], scene_rows: list[dict], batch_size: int, now: int) -> int:
        if not examples or self.example_manager is None or self.example_selector is None:
            return 0
        try:
            selected = await self.example_selector.choose(
                examples,
                scene=self.render(scene_rows),
                persona=self.persona(),
            )
            if selected is None:
                return 0
            self.store.record(
                scope, [selected], persona_hash=persona_digest(self.persona()),
                batch_size=batch_size, now=now,
            )
            row = self.store.pending_suggestion(scope, selected["suggestion"])
            if row is None:
                return 0
            changed = self.example_manager.apply(row)
            self.store.decide(row["id"], "accepted", note="自动风格演进", now=now)
            if changed:
                log.info("persona example evolved scope=%s proposal=%s", scope, row["id"])
            return int(changed)
        except (ValueError, OSError):
            log.exception("persona example update refused for %s", scope)
            return 0
        except Exception:
            log.exception("persona example selection failed for %s", scope)
            return 0

    async def run(self, *, sleep=asyncio.sleep) -> None:
        while True:
            for scope in self.scopes:
                try:
                    await self.propose_for(scope)
                except Exception:
                    # One bad scope must not stop the others.
                    log.exception("persona proposal pass failed for %s", scope)
            await sleep(max(60, self.config.proposal_interval_hours * 600))
