"""Optional local Wiki runtime composition."""

from __future__ import annotations

import os
from pathlib import Path

from qqbot.agent.memory.wiki.cache import WikiContextCache
from qqbot.agent.memory.wiki.reader import WikiReader
from qqbot.infrastructure.storage.wiki_files import MarkdownWikiStore


def configured_wiki_reader() -> WikiReader | None:
    """Build the reader only when the deployer explicitly enables Wiki."""
    if os.getenv("BOT_WIKI_ENABLED", "false").strip().lower() != "true":
        return None
    root = Path(os.getenv("BOT_WIKI_ROOT", "./data/wiki"))
    try:
        ttl = max(0, int(os.getenv("BOT_WIKI_CONTEXT_TTL_SECONDS", "300")))
    except ValueError as exc:
        raise ValueError("BOT_WIKI_CONTEXT_TTL_SECONDS must be an integer") from exc
    pages = MarkdownWikiStore(root).load()
    return WikiReader(pages, cache=WikiContextCache(ttl_seconds=ttl))

