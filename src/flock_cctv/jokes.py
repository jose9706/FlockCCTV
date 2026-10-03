"""Short, statistics-based jokes for the Leland Tracker."""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any


def _duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{total_seconds}s"


def make_roast(
    statistics: dict[str, Any],
    period_label: str,
    *,
    chooser: Any = random.choice,
) -> str | None:
    """Return a gentle joke based on a real, nonzero statistic, or ``None``.

    ``period_label`` is supplied by the command from a fixed set of labels; the
    function never accepts or interpolates user-provided Discord text.
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
    return chooser(options)


class SharedRoastCooldown:
    """One process-wide cooldown shared by every user of ``/leland roast``."""

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
