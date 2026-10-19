"""Bounded in-memory log ring for the owner console.

The bot logs to stdout, which is invisible from a browser on another machine.
A ring keeps the last few hundred records so the console can show what the
process is complaining about right now. It is deliberately bounded: an operator
console must never be the reason a long-running process grows.

The ring is a singleton per process because two consoles (or a console plus a
test) must not stack handlers on the same logger and double every line.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

CAPACITY = 500


class LogRing(logging.Handler):
    def __init__(self, capacity=CAPACITY):
        super().__init__(level=logging.INFO)
        self._records: deque[dict] = deque(maxlen=max(50, int(capacity)))
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            entry = {
                "at": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage()[:500],
            }
        except Exception:
            return
        with self._lock:
            self._records.append(entry)

    def tail(self, limit=200):
        with self._lock:
            items = list(self._records)
        return items[-int(limit):][::-1]


_ring: LogRing | None = None
_install_lock = threading.Lock()


def install(logger_name="qqbot", level=logging.INFO):
    global _ring
    with _install_lock:
        if _ring is None:
            _ring = LogRing()
            _ring.setLevel(level)
            logging.getLogger(logger_name).addHandler(_ring)
        return _ring
