"""Shared SQLite connection; each repository owns its own schema migration."""

from __future__ import annotations

import pkgutil
import sqlite3
import threading
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path


def migration_modules() -> list:
    """Every storage module that declares a ``migrate(db)`` hook.

    Discovered rather than listed: a new repository adds its own table by
    dropping a file in this package with a ``migrate`` function, and never has
    to edit this one. Alphabetical order keeps the sequence deterministic —
    the tables are independent, so no ordering constraint exists.
    """
    package_dir = str(Path(__file__).resolve().parent)
    modules = []
    for info in sorted(pkgutil.iter_modules([package_dir]), key=lambda i: i.name):
        if info.name == "base":
            continue
        module = import_module(f"{__package__}.{info.name}")
        if callable(getattr(module, "migrate", None)):
            modules.append(module)
    return modules


class SqliteDatabase:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # Memory evidence, revisions and topic links are aggregate children.
        # SQLite leaves foreign-key enforcement off per connection unless the
        # application enables it explicitly.
        self.db.execute("PRAGMA foreign_keys=ON")
        # The scheduler runs on the asyncio thread while the owner WebUI uses
        # request threads. One SQLite connection may be shared, but two BEGIN
        # statements may not overlap on it.
        self._transaction_lock = threading.RLock()
        for module in migration_modules():
            module.migrate(self.db)

    @contextmanager
    def transaction(self):
        with self._transaction_lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def close(self) -> None:
        self.db.close()
