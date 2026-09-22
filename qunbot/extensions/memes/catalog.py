"""Local-only meme catalog; paths may not escape the configured root."""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path


class LocalMemeCatalog:
    def __init__(self, root: Path):
        self.root = root.resolve()
        manifest = self.root / "catalog.json"
        raw = (
            json.loads(manifest.read_text(encoding="utf-8"))
            if manifest.exists()
            else {"memes": []}
        )
        self.entries: dict[str, list[Path]] = {}
        for item in raw.get("memes", []):
            tag = str(item["tag"])
            relative = Path(str(item["file"]))
            target = (self.root / relative).resolve()
            if not target.is_relative_to(self.root) or target.suffix.lower() not in {
                ".png", ".jpg", ".jpeg", ".gif", ".webp",
            }:
                raise ValueError(f"invalid meme file path: {relative}")
            if target.is_file():
                self.entries.setdefault(tag, []).append(target)

    def pick(self, tag: str) -> str | None:
        files = self.entries.get(tag, [])
        if not files:
            return None
        data = random.choice(files).read_bytes()
        if len(data) > 4 * 1024 * 1024:
            return None
        return "base64://" + base64.b64encode(data).decode("ascii")

    def available_tags(self) -> list[str]:
        return sorted(self.entries)
