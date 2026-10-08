"""Owner-approved, recoverable replacement of the persona example window."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from pathlib import Path

from ...storage.persona_review import persona_digest

START = "<!-- persona-examples:start -->"
END = "<!-- persona-examples:end -->"
MAX_EXAMPLES = 8
_HEADING = re.compile(r"(?m)^\*\*[^\n]+\*\*[ \t]*$")


class PersonaExampleManager:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def _dialogue(value: str) -> tuple[str, str]:
        lines = str(value or "").splitlines()
        if len(lines) != 2 or not lines[0].startswith("群友：") or not lines[1].startswith("Kinna："):
            raise ValueError("示例必须是一组场景提示与 Kinna 的真实短回复")
        user, assistant = lines[0][3:].strip(), lines[1][6:].strip()
        if not 4 <= len(user) <= 80 or not 4 <= len(assistant) <= 90:
            raise ValueError("示例对话长度不合适")
        if any(mark in user + assistant for mark in ("<", ">", "{", "}", "`")):
            raise ValueError("示例对话含有不允许的标记")
        return user, assistant

    @staticmethod
    def _blocks(text: str) -> list[str]:
        headings = list(_HEADING.finditer(text))
        if not headings or headings[0].start() != 0:
            raise ValueError("人格示例区格式无法识别，未改动文件")
        return [
            text[item.start():headings[index + 1].start() if index + 1 < len(headings) else len(text)].strip()
            for index, item in enumerate(headings)
        ]

    def apply(self, proposal: dict) -> bool:
        """Apply one reviewed example. True if the file changed."""
        if proposal.get("kind") != "example":
            raise ValueError("这不是示例对话提议")
        proposal_id = int(proposal["id"])
        user, assistant = self._dialogue(str(proposal.get("suggestion") or ""))
        marker = f"<!-- persona-example-id:{proposal_id} -->"
        with self._lock:
            original = self.path.read_text(encoding="utf-8")
            if marker in original:
                return False
            expected = str(proposal.get("persona_hash") or "")
            if expected and persona_digest(original) != expected:
                raise ValueError("人格文件已变化，请重新生成示例候选后再采用")
            if original.count(START) != 1 or original.count(END) != 1:
                raise ValueError("人格文件缺少唯一的示例管理区，未改动文件")
            before, remainder = original.split(START, 1)
            inside, after = remainder.split(END, 1)
            blocks = self._blocks(inside.strip())
            candidate = (
                f"**从真实对话中留下的接法**\n\n"
                f"群友：{user}\n\nKinna：{assistant}\n\n"
                f"只学这次的节奏，不重复原话。\n\n{marker}"
            )
            blocks = [*blocks, candidate][-MAX_EXAMPLES:]
            updated = before + START + "\n" + "\n\n".join(blocks) + "\n" + END + after
            # A backup is kept before every rotation, including the examples
            # retired by this approval. The caller only marks the row accepted
            # after this write succeeds.
            history = self.path.parent / "persona-history"
            history.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
            backup = history / f"{stamp}-{persona_digest(original)}.md"
            if not backup.exists():
                backup.write_text(original, encoding="utf-8")
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=".persona-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                try:
                    handle.write(updated)
                    handle.flush()
                    os.fsync(handle.fileno())
                except Exception:
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                temporary.chmod(self.path.stat().st_mode & 0o777)
                # Refuse a concurrent manual edit instead of overwriting it.
                if self.path.read_text(encoding="utf-8") != original:
                    raise ValueError("人格文件刚被其他操作修改，请刷新后重试")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return True
