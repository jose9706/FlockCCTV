"""Date and interval helpers shared by storage queries.

All public timestamps in this package are UTC Unix seconds. Calendar grouping
uses the configured IANA timezone, including daylight-saving transitions.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterable
from zoneinfo import ZoneInfo


def get_timezone(value: str | tzinfo) -> tzinfo:
    """Return a timezone object from an IANA name or an existing tzinfo."""
    if isinstance(value, str):
        return ZoneInfo(value)
    if not isinstance(value, tzinfo):
        raise TypeError("timezone must be an IANA name or tzinfo")
    return value


def local_date(timestamp: float, timezone_name: str | tzinfo) -> date:
    """Return the local calendar date containing a UTC Unix timestamp."""
    return datetime.fromtimestamp(timestamp, tz=get_timezone(timezone_name)).date()


def local_day(timestamp: float, timezone_name: str | tzinfo) -> str:
    """Return a zero-padded ISO local date for a UTC Unix timestamp."""
    return local_date(timestamp, timezone_name).isoformat()


def local_midnight(day: date, timezone_name: str | tzinfo) -> float:
    """Return the UTC Unix timestamp of local midnight on ``day``."""
    zone = get_timezone(timezone_name)
    return datetime.combine(day, time.min, tzinfo=zone).timestamp()


def split_interval_by_day(
    start: float, end: float, timezone_name: str | tzinfo
) -> list[tuple[str, float]]:
    """Split a half-open UTC interval into local-day duration pieces.

    The returned durations sum to ``max(0, end - start)`` (within floating
    point precision). A local day can be 23 or 25 hours across DST changes.
    """
    if end <= start:
        return []

    zone = get_timezone(timezone_name)
    pieces: list[tuple[str, float]] = []
    cursor = start
    while cursor < end:
        day = datetime.fromtimestamp(cursor, tz=zone).date()
        next_midnight = local_midnight(day + timedelta(days=1), zone)
        boundary = min(end, next_midnight)
        # Defend against unusual timezone histories with a skipped local date.
        if boundary <= cursor:
            boundary = end
        pieces.append((day.isoformat(), boundary - cursor))
        cursor = boundary
    return pieces


def period_bounds(
    period: str,
    now: float,
    timezone_name: str | tzinfo,
    tracking_since: float,
) -> tuple[float, float]:
    """Return UTC bounds ``(start, end)`` for a supported reporting period.

    ``week`` begins Monday at local midnight. ``last7`` covers the last seven
    local days, today included, from local midnight six days ago. ``all`` begins at the persisted
    observation start, which may be later than the current database's age.
    """
    zone = get_timezone(timezone_name)
    local_now = datetime.fromtimestamp(now, tz=zone)
    local_today = local_now.date()

    if period == "today":
        start_day = local_today
    elif period == "week":
        start_day = local_today - timedelta(days=local_today.weekday())
    elif period == "last7":
        start_day = local_today - timedelta(days=6)
    elif period == "month":
        start_day = local_today.replace(day=1)
    elif period == "all":
        return tracking_since, now
    else:
        raise ValueError(f"unsupported period: {period}")

    return local_midnight(start_day, zone), now


def previous_period_bounds(
    period: str, now: float, timezone_name: str | tzinfo
) -> tuple[float, float] | None:
    """Return the previous day/week/seven days/month up to the same local point.

    ``None`` for ``all``, which has no previous period. The previous window
    ends at the same day offset and wall-clock time, so a DST change does not
    shift it by an hour. It is clipped to end where the current period starts,
    so a long month is never compared with time from the current one.
    """
    if period == "all":
        return None
    zone = get_timezone(timezone_name)
    start, _ = period_bounds(period, now, zone, now)
    local_now = datetime.fromtimestamp(now, tz=zone)
    start_day = datetime.fromtimestamp(start, tz=zone).date()
    if period == "today":
        previous_day = start_day - timedelta(days=1)
    elif period in ("week", "last7"):
        previous_day = start_day - timedelta(days=7)
    else:
        previous_day = (start_day - timedelta(days=1)).replace(day=1)
    previous_start = local_midnight(previous_day, zone)
    same_day = previous_day + (local_now.date() - start_day)
    same_point = datetime.combine(same_day, local_now.time(), tzinfo=zone).timestamp()
    return previous_start, max(previous_start, min(start, same_point))


def split_interval_by_hour(
    start: float, end: float, timezone_name: str | tzinfo
) -> list[tuple[int, float]]:
    """Split a half-open UTC interval into ``(local hour, seconds)`` pieces."""
    zone = get_timezone(timezone_name)
    pieces: list[tuple[int, float]] = []
    cursor = start
    while cursor < end:
        local = datetime.fromtimestamp(cursor, tz=zone)
        floor = local.replace(minute=0, second=0, microsecond=0)
        boundary = min(end, (floor + timedelta(hours=1)).timestamp())
        # Repeated or skipped wall-clock hours can put the boundary behind us.
        if boundary <= cursor:
            boundary = min(end, cursor + 3600.0)
        pieces.append((local.hour, boundary - cursor))
        cursor = boundary
    return pieces


def interval_overlap(start: float, end: float, window_start: float, window_end: float) -> float:
    """Return the overlap in seconds between two half-open intervals."""
    return max(0.0, min(end, window_end) - max(start, window_start))


def merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Return sorted, non-overlapping half-open intervals; empty ones are dropped."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(item for item in intervals if item[1] > item[0]):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def intersect_intervals(
    first: Iterable[tuple[float, float]], second: Iterable[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Return the sorted overlap of two sets of half-open intervals."""
    left = merge_intervals(first)
    right = merge_intervals(second)
    result: list[tuple[float, float]] = []
    i = j = 0
    while i < len(left) and j < len(right):
        start = max(left[i][0], right[j][0])
        end = min(left[i][1], right[j][1])
        if end > start:
            result.append((start, end))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return result


__all__ = [
    "get_timezone",
    "intersect_intervals",
    "interval_overlap",
    "local_date",
    "local_day",
    "local_midnight",
    "merge_intervals",
    "period_bounds",
    "previous_period_bounds",
    "split_interval_by_day",
    "split_interval_by_hour",
]
