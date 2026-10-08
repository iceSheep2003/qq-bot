"""Local-only meme catalog; paths may not escape the configured root."""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path


_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})


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
            if not target.is_relative_to(self.root) or target.suffix.lower() not in _IMAGE_EXTENSIONS:
                raise ValueError(f"invalid meme file path: {relative}")
            if target.is_file():
                self.entries.setdefault(tag, []).append(target)
        for pack in raw.get("packs", []):
            pack_root = (self.root / str(pack["root"])).resolve()
            if not pack_root.is_relative_to(self.root):
                raise ValueError(f"invalid meme pack root: {pack['root']}")
            if not pack_root.is_dir():
                continue
            for category, tag in pack["category_tags"].items():
                category_root = (pack_root / str(category)).resolve()
                if not category_root.is_relative_to(pack_root):
                    raise ValueError(f"invalid meme pack category: {category}")
                if not category_root.is_dir():
                    continue
                for file in sorted(category_root.iterdir()):
                    target = file.resolve()
                    if (target.is_relative_to(category_root) and target.is_file()
                            and target.suffix.lower() in _IMAGE_EXTENSIONS):
                        self.entries.setdefault(str(tag), []).append(target)

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
