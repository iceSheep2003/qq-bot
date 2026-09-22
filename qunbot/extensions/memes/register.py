"""Meme selection is an optional media contribution."""

import os
from pathlib import Path

from .catalog import LocalMemeCatalog


def register(host, _config, _model) -> None:
    catalog = LocalMemeCatalog(Path(os.getenv("BOT_MEMES_PATH", "./memes")))
    host.meme_source = catalog
    host.context.register("available_meme_tags", lambda _event: catalog.available_tags())


def validate() -> dict:
    catalog = LocalMemeCatalog(Path(os.getenv("BOT_MEMES_PATH", "./memes")))
    return {"tags": catalog.available_tags()}
