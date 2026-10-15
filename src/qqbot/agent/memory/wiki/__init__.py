"""Markdown Wiki domain contracts and short-lived context caching."""

from .cache import WikiContextCache
from .page import (
    WikiEntry,
    WikiLink,
    WikiPage,
    WikiRelation,
    WikiSlang,
    WikiTopic,
)
from .links import LinkCandidate, markdown_link, reciprocal
from .update_policy import WikiUpdatePolicy, published_version
from .reader import WikiReader, read_mode_for_query
from .writer import WikiWriter
from .legacy import LegacyWikiProjector
from .migration import WikiMigration, WikiMigrationReport

__all__ = [
    "WikiContextCache",
    "WikiEntry",
    "WikiLink",
    "WikiPage",
    "WikiRelation",
    "WikiSlang",
    "WikiTopic",
    "LinkCandidate",
    "markdown_link",
    "reciprocal",
    "WikiUpdatePolicy",
    "published_version",
    "WikiReader",
    "read_mode_for_query",
    "WikiWriter",
    "LegacyWikiProjector",
    "WikiMigration",
    "WikiMigrationReport",
]
