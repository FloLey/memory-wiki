"""In-memory ring buffer for recent server logs, surfaced at /ui/logs.

A logging handler keeps the last few hundred records from the ``wiki_server``
logger namespace in memory (same process as the server), so the console can show
recent activity and errors without a database or a log file. Nothing sensitive is
logged; the page is owner-only like the rest of /ui.
"""

from __future__ import annotations

import collections
import datetime
import logging
import threading

_BUFFER: collections.deque = collections.deque(maxlen=500)
_LOCK = threading.Lock()  # guards reads/writes: the scheduler thread appends while
# request threads read, and iterating a deque mid-mutation can raise.
_installed = False


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "time": datetime.datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "msg": self.format(record),  # includes the traceback for exceptions
            }
            with _LOCK:
                _BUFFER.append(entry)
        except Exception:
            pass


def setup_logging(level: int = logging.INFO) -> None:
    """Attach the ring-buffer handler to the wiki_server logger (idempotent)."""
    global _installed
    if _installed:
        return
    logger = logging.getLogger("wiki_server")
    logger.setLevel(level)
    handler = _RingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _installed = True


def recent_logs(limit: int = 200) -> list[dict]:
    """The most recent log records, newest first."""
    with _LOCK:
        items = list(_BUFFER)
    return items[-limit:][::-1]
