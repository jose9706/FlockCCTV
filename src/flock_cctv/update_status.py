"""Read the Pi updater's status and report it to tracker admins.

The root-run updater (``deploy/update.py``) writes ``update-status.json`` next
to the database after every poll. The bot only reads it, and writes its own
``bot-ready.json`` marker so the updater can confirm a new version reached
Discord. Neither file holds tracked statistics or credentials.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

STATUS_NAME = "update-status.json"
READY_NAME = "bot-ready.json"
NOTIFIED_NAME = "update-notified.json"
# A systemd path unit starts the updater when this appears; the updater removes it.
REQUEST_NAME = "update-requested.json"


def status_path(database_path: Path) -> Path:
    return database_path.with_name(STATUS_NAME)


def read_status(database_path: Path) -> dict[str, Any] | None:
    """Return the updater's last status, or ``None`` when absent or unreadable."""
    try:
        value = json.loads(status_path(database_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


def write_ready(database_path: Path, now: float | None = None) -> None:
    """Record that this process connected to Discord, for the updater's health check."""
    _write_json(
        database_path.with_name(READY_NAME),
        {"ready_at": time.time() if now is None else now, "pid": os.getpid()},
    )


def request_update(database_path: Path, now: float | None = None) -> None:
    """Ask the updater to check for a new commit now instead of at the next poll."""
    _write_json(
        database_path.with_name(REQUEST_NAME),
        {"requested_at": time.time() if now is None else now},
    )


def _when(timestamp: Any, timezone: str) -> str:
    try:
        local = datetime.fromtimestamp(float(timestamp), tz=ZoneInfo(timezone))
    except (TypeError, ValueError, OverflowError):
        return "an unknown time"
    return local.strftime("%b %-d, %Y %H:%M %Z")


def _short(revision: Any) -> str:
    return str(revision)[:7] if revision else "unknown"


def status_line(status: dict[str, Any] | None, timezone: str) -> str:
    """Summarize the last update check for ``/leland about``."""
    if status is None:
        return "Auto-update: **no status recorded** (the updater has not run since this was installed)."
    checked = _when(status.get("checked_at"), timezone)
    result = status.get("result")
    if result == "failed":
        failures = int(status.get("failures") or 0)
        attempts = f"{failures} attempt{'s' if failures != 1 else ''}"
        message = str(status.get("message") or "no details recorded")
        return (
            f"Auto-update: **failing** since {_when(status.get('failing_since'), timezone)} "
            f"({attempts}; last check {checked}). Error: {message}"
        )
    if result == "held":
        return f"Auto-update: **held** at `{_short(status.get('revision'))}`; last check {checked}."
    if result == "updated":
        return f"Auto-update: **updated** to `{_short(status.get('revision'))}` at {checked}."
    return f"Auto-update: **up to date** at `{_short(status.get('revision'))}`; last check {checked}."


def alert_due(status: dict[str, Any] | None, database_path: Path) -> bool:
    """True once per failure streak, even across bot restarts."""
    if status is None or status.get("result") != "failed" or status.get("failing_since") is None:
        return False
    try:
        notified = json.loads(database_path.with_name(NOTIFIED_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        notified = {}
    return not isinstance(notified, dict) or notified.get("failing_since") != status["failing_since"]


def mark_alerted(status: dict[str, Any], database_path: Path) -> None:
    _write_json(database_path.with_name(NOTIFIED_NAME), {"failing_since": status["failing_since"]})


def alert_text(status: dict[str, Any], timezone: str) -> str:
    return (
        "Leland Tracker automatic updates are failing.\n"
        + status_line(status, timezone)
        + "\nOn the Pi: `sudo journalctl -u flock-cctv-update.service -n 100 --no-pager`. "
        "You'll get one message per failure streak; `/leland about` shows the latest."
    )


__all__ = [
    "alert_due",
    "alert_text",
    "mark_alerted",
    "read_status",
    "request_update",
    "status_line",
    "write_ready",
]
