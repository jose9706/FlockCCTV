from __future__ import annotations

import unittest
from datetime import datetime, timezone

from flock_cctv.stats import (
    interval_overlap,
    local_day,
    period_bounds,
    previous_period_bounds,
    split_interval_by_day,
    split_interval_by_hour,
)


def epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class DateHelpersTests(unittest.TestCase):
    def test_previous_period_matches_elapsed_time_and_stops_at_current_start(self) -> None:
        now = epoch("2025-03-05T18:30:00")  # Wednesday 12:30 in Costa Rica
        zone = "America/Costa_Rica"
        self.assertEqual(
            previous_period_bounds("today", now, zone),
            (epoch("2025-03-04T06:00:00"), epoch("2025-03-04T18:30:00")),
        )
        self.assertEqual(
            previous_period_bounds("week", now, zone),
            (epoch("2025-02-24T06:00:00"), epoch("2025-02-26T18:30:00")),
        )
        self.assertEqual(
            previous_period_bounds("last7", now, zone),
            (epoch("2025-02-20T06:00:00"), epoch("2025-02-26T18:30:00")),
        )
        self.assertEqual(
            previous_period_bounds("month", now, zone),
            (epoch("2025-02-01T06:00:00"), epoch("2025-02-05T18:30:00")),
        )
        self.assertIsNone(previous_period_bounds("all", now, zone))
        # March 31 is longer than February, so the window stops at March 1.
        late = epoch("2025-03-31T18:00:00")
        self.assertEqual(
            previous_period_bounds("month", late, zone),
            (epoch("2025-02-01T06:00:00"), epoch("2025-03-01T06:00:00")),
        )

    def test_previous_period_keeps_wall_clock_time_across_dst(self) -> None:
        zone = "America/New_York"
        # Monday after spring-forward: yesterday 00:00-10:00 local is only 9 real hours.
        self.assertEqual(
            previous_period_bounds("today", epoch("2026-03-09T14:00:00"), zone),
            (epoch("2026-03-08T05:00:00"), epoch("2026-03-08T14:00:00")),
        )
        # Monday after fall-back: yesterday 00:00-10:00 local is 11 real hours.
        self.assertEqual(
            previous_period_bounds("today", epoch("2026-11-02T15:00:00"), zone),
            (epoch("2026-11-01T04:00:00"), epoch("2026-11-01T15:00:00")),
        )

    def test_hour_split_uses_local_hours_including_half_hour_zones(self) -> None:
        self.assertEqual(
            split_interval_by_hour(epoch("2025-03-05T03:30:00"), epoch("2025-03-05T05:15:00"), "America/Costa_Rica"),
            [(21, 1800.0), (22, 3600.0), (23, 900.0)],
        )
        self.assertEqual(
            split_interval_by_hour(epoch("2025-03-05T00:00:00"), epoch("2025-03-05T01:00:00"), "Asia/Kolkata"),
            [(5, 1800.0), (6, 1800.0)],
        )
        self.assertEqual(split_interval_by_hour(10.0, 10.0, "UTC"), [])
        pieces = split_interval_by_hour(
            epoch("2025-11-02T04:30:00"), epoch("2025-11-02T07:30:00"), "America/New_York"
        )
        # The repeated 01:00 hour on the fall-back night lasts two real hours.
        self.assertEqual(pieces, [(0, 1800.0), (1, 7200.0), (2, 1800.0)])

    def test_calendar_periods_use_local_midnight_and_monday_week_start(self) -> None:
        now = epoch("2025-02-05T18:30:00")
        self.assertEqual(
            period_bounds("today", now, "America/Costa_Rica", 0),
            (epoch("2025-02-05T06:00:00"), now),
        )
        self.assertEqual(
            period_bounds("week", now, "America/Costa_Rica", 0),
            (epoch("2025-02-03T06:00:00"), now),
        )
        # The rolling window keeps seven local days even early in the week.
        self.assertEqual(
            period_bounds("last7", now, "America/Costa_Rica", 0),
            (epoch("2025-01-30T06:00:00"), now),
        )
        self.assertEqual(
            period_bounds("month", now, "America/Costa_Rica", 0),
            (epoch("2025-02-01T06:00:00"), now),
        )
        self.assertEqual(period_bounds("all", now, "UTC", 12), (12, now))

    def test_interval_splits_at_local_midnight(self) -> None:
        start = epoch("2025-03-02T23:50:00")
        end = epoch("2025-03-03T00:10:00")
        self.assertEqual(
            split_interval_by_day(start, end, "UTC"),
            [("2025-03-02", 600.0), ("2025-03-03", 600.0)],
        )
        self.assertEqual(local_day(start, "UTC"), "2025-03-02")
        self.assertEqual(split_interval_by_day(end, start, "UTC"), [])

    def test_interval_respects_dst_day_length(self) -> None:
        start = epoch("2025-03-09T04:00:00")
        end = epoch("2025-03-10T04:00:00")
        pieces = split_interval_by_day(start, end, "America/New_York")
        self.assertEqual(pieces, [("2025-03-08", 3600.0), ("2025-03-09", 82800.0)])

    def test_overlap_is_clipped_and_never_negative(self) -> None:
        self.assertEqual(interval_overlap(0, 10, 4, 14), 6)
        self.assertEqual(interval_overlap(0, 4, 5, 10), 0)

    def test_unknown_period_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            period_bounds("year", 100, "UTC", 0)


if __name__ == "__main__":
    unittest.main()
