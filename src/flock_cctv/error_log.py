"""Collect the bot's own warnings and errors for the admin error log.

The handler only buffers. The bot writes buffered entries to the Store from its
checkpoint loop, so logging never waits on SQLite and a failing Store cannot
recurse into more log records.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

# Entries kept in memory while the Store is unavailable; older ones are dropped.
PENDING_LIMIT = 200
SUMMARY_LIMIT = 200

Entry = tuple[float, str, str, str]


def _summary(record: logging.LogRecord) -> str:
    """Return the log line and exception type, never exception text.

    The bot's own log lines are fixed sentences with command names at most;
    exception messages can carry anything a dependency put in them.
    """
    try:
        text = record.getMessage()
    except Exception:
        text = str(record.msg)
    text = " ".join(text.split())[:SUMMARY_LIMIT]
    if record.exc_info and record.exc_info[0] is not None:
        text += f" ({record.exc_info[0].__name__})"
    return text


class ErrorLogBuffer(logging.Handler):
    """Buffer WARNING and higher records from the ``flock_cctv`` loggers."""

    def __init__(self, capacity: int = PENDING_LIMIT) -> None:
        super().__init__(logging.WARNING)
        self._pending: deque[Entry] = deque(maxlen=capacity)
        self._entries_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            source = record.name.removeprefix("flock_cctv.")
            entry = (float(record.created), record.levelname, source, _summary(record))
            with self._entries_lock:
                self._pending.append(entry)
        except Exception:
            self.handleError(record)

    def drain(self) -> list[Entry]:
        """Remove and return everything buffered so far, oldest first."""
        with self._entries_lock:
            entries = list(self._pending)
            self._pending.clear()
        return entries

    def restore(self, entries: list[Entry]) -> None:
        """Put entries back after a failed write, ahead of anything newer."""
        with self._entries_lock:
            newer = list(self._pending)
            self._pending.clear()
            self._pending.extend(entries)
            self._pending.extend(newer)


__all__ = ["ErrorLogBuffer"]
