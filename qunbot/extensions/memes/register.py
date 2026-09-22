"""Meme selection is an optional media contribution."""

import os
from pathlib import Path

from ...runtime.context import Trust
from .catalog import LocalMemeCatalog


def register(host, _config, _model) -> None:
    catalog = LocalMemeCatalog(Path(os.getenv("BOT_MEMES_PATH", "./memes")))
    host.meme_source = catalog
    # The deployer's own asset catalogue, so it carries more authority than
    # anything the model produced. High priority: it is the short, fixed list
    # the model needs to emit a valid marker.
    host.context.register(
        "available_meme_tags",
        lambda _event: catalog.available_tags(),
        trust=Trust.DEPLOYER,
        priority=20,
        max_chars=300,
    )


def validate() -> dict:
    catalog = LocalMemeCatalog(Path(os.getenv("BOT_MEMES_PATH", "./memes")))
    return {"tags": catalog.available_tags()}
