"""Text formatting shared by reports, jokes, and updater status lines."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import discord

from .stats import get_timezone


def clean_name(value: Any) -> str:
    """Collapse whitespace (including newlines) in a display name and shorten it."""
    return " ".join(str(value).split())[:48] if value else ""


def safe_name(name: str) -> str:
    """Make a cleaned name safe to embed in Markdown text without pinging anyone."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(name))


def duration(seconds: float) -> str:
    """Format seconds as ``1d 2h 3m``, ``4m``, or ``5s``."""
    whole = max(0, int(seconds))
    days, remain = divmod(whole, 86_400)
    hours, remain = divmod(remain, 3_600)
    minutes, secs = divmod(remain, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def local_time(timestamp: float | None, timezone: str, *, date_only: bool = False) -> str:
    """Format a UTC timestamp in ``timezone``, or "not recorded" for ``None``."""
    if timestamp is None:
        return "not recorded"
    local = datetime.fromtimestamp(float(timestamp), tz=get_timezone(timezone))
    return local.strftime("%b %-d, %Y" if date_only else "%b %-d, %Y %H:%M %Z")


__all__ = ["clean_name", "duration", "local_time", "safe_name"]
