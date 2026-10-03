"""Short, statistics-based jokes for the Flock tracker."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import discord


def _duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{total_seconds}s"


def _addressee(name: str | None) -> str | None:
    """Return a display name safe to embed in a message, or ``None`` when blank.

    Whitespace (including newlines) is collapsed and the name is shortened, then
    Markdown and mentions are escaped so it can neither format nor ping.
    """
    cleaned = " ".join(str(name).split())[:48] if name else ""
    if not cleaned:
        return None
    return discord.utils.escape_mentions(discord.utils.escape_markdown(cleaned))


def make_roast(
    statistics: dict[str, Any],
    period_label: str,
    *,
    name: str | None = None,
    chooser: Any = random.choice,
) -> str | None:
    """Return a gentle joke based on a real, nonzero statistic, or ``None``.

    ``period_label`` is supplied by the command from a fixed set of labels. The
    only user-provided Discord text accepted is the optional display ``name`` of
    the person being roasted, which is escaped here and never interpolated raw.
    """
    options: list[str] = []
    messages = int(statistics.get("messages", 0) or 0)
    voice_seconds = float(statistics.get("voice_seconds", 0) or 0)
    active_days = int(statistics.get("active_days", 0) or 0)

    if messages > 0:
        options.append(
            f"{messages:,} messages {period_label}? Your keyboard deserves a lunch break."
        )
    if voice_seconds > 0:
        options.append(
            f"{_duration(voice_seconds)} in observed voice {period_label}; the headset is earning its keep."
        )
    if active_days > 0:
        options.append(
            f"{active_days} active days {period_label}. That is a very committed punch card."
        )

    if not options:
        return None
    joke = chooser(options)
    addressee = _addressee(name)
    # Every joke opens with a recorded number, so a leading name reads naturally.
    return joke if addressee is None else f"{addressee}, {joke}"


class SharedRoastCooldown:
    """One process-wide cooldown shared by every user of ``/flock roast``."""

    def __init__(self, seconds: float = 30.0) -> None:
        if seconds <= 0:
            raise ValueError("cooldown seconds must be positive")
        self.seconds = seconds
        self._available_at = 0.0
        self._lock = asyncio.Lock()

    async def consume(self, now: float | None = None) -> float:
        """Consume a slot and return remaining seconds (zero when accepted)."""
        current = time.monotonic() if now is None else now
        async with self._lock:
            remaining = self._available_at - current
            if remaining > 0:
                return remaining
            self._available_at = current + self.seconds
            return 0.0
