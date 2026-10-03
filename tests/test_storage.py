from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flock_cctv.storage import Store, StoreError


USER = 22
OTHER = 23
THIRD = 24


def epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "tracker.sqlite3"
        self.backups = self.root / "backups"
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(100.0, 11)
        self.assertTrue(await self.store.track_user(USER, 99, 100.0))

    async def new_store(
        self, name: str, now: float, timezone_name: str = "UTC", *users: int
    ) -> Store:
        """Open a second database whose tracked people are tracked from ``now``."""
        store = Store(self.root / f"{name}.sqlite3", self.root / f"{name}-backups", timezone_name)
        await store.initialize(now, 11)
        for user in users or (USER,):
            await store.track_user(user, 99, now)
        return store

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    async def test_messages_deduplicate_update_days_and_enforce_pause_boundary(self) -> None:
        stamp = epoch("2025-02-03T12:00:00")
        self.assertTrue(await self.store.add_message(USER, 1, 30, stamp))
        self.assertFalse(await self.store.add_message(USER, 1, 30, stamp))
        self.assertTrue(await self.store.add_message(USER, 2, 30, stamp + 1))
        self.assertEqual((await self.store.stats(USER, "all", stamp + 2))["messages"], 2)
        self.assertEqual(await self.store.records(USER, stamp + 2), {
            "busiest_day": "2025-02-03",
            "busiest_day_messages": 2,
            "longest_visit_seconds": 0.0,
            "longest_visit_at": None,
            "current_visit_seconds": None,
            "current_visit_complete_start": False,
        })

        await self.store.set_paused(True, 99, stamp + 3)
        self.assertFalse(await self.store.add_message(USER, 3, 30, stamp + 4))
        await self.store.set_paused(False, 99, stamp + 10)
        self.assertFalse(await self.store.add_message(USER, 4, 30, stamp + 9))
        self.assertTrue(await self.store.add_message(USER, 5, 30, stamp + 10))
        self.assertEqual((await self.store.stats(USER, "all", stamp + 11))["messages"], 3)

    async def test_evil_mode_persists_and_deletion_disables_it(self) -> None:
        self.assertFalse((await self.store.state())["evil_mode"])
        await self.store.set_evil_mode(True)
        self.assertTrue((await self.store.state())["evil_mode"])
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200.0, 11)
        self.assertTrue((await self.store.state())["evil_mode"])
        await self.store.delete_data(99, 210.0)
        self.assertFalse((await self.store.state())["evil_mode"])

    async def test_reaction_mode_counts_only_when_enabled_and_survives_restart(self) -> None:
        self.assertFalse((await self.store.state())["reaction_mode"])
        self.assertEqual(
            await self.store.add_message_with_reaction(USER, 1, 30, 101.0, ordinary=True),
            (True, False),
        )
        with patch("flock_cctv.storage.random.randint", return_value=15):
            await self.store.set_reaction_mode(True)
            self.assertEqual(
                await self.store.add_message_with_reaction(USER, 2, 30, 102.0, ordinary=False),
                (True, False),
            )
            for message_id in range(3, 10):
                self.assertEqual(
                    await self.store.add_message_with_reaction(USER, message_id, 30, float(message_id + 100), ordinary=True),
                    (True, False),
                )
            await self.store.close()
            self.store = Store(self.db, self.backups, "UTC")
            await self.store.initialize(200.0, 11)
            self.assertTrue((await self.store.state())["reaction_mode"])
            self.assertEqual(
                await self.store.add_message_with_reaction(USER, 9, 30, 109.0, ordinary=True),
                (False, False),
            )
            for message_id in range(10, 17):
                self.assertEqual(
                    await self.store.add_message_with_reaction(USER, message_id, 30, float(message_id + 100), ordinary=True),
                    (True, False),
                )
            # A crash after this commit but before Discord accepts a reaction
            # cannot make the replay decrement the next interval twice.
            self.assertEqual(
                await self.store.add_message_with_reaction(USER, 17, 30, 117.0, ordinary=True),
                (True, True),
            )
            await self.store.close()
            self.store = Store(self.db, self.backups, "UTC")
            await self.store.initialize(220.0, 11)
            self.assertEqual(
                await self.store.add_message_with_reaction(USER, 17, 30, 117.0, ordinary=True),
                (False, False),
            )
            for message_id in range(18, 32):
                self.assertEqual(
                    await self.store.add_message_with_reaction(USER, message_id, 30, float(message_id + 100), ordinary=True),
                    (True, False),
                )
            self.assertEqual(
                await self.store.add_message_with_reaction(USER, 32, 30, 132.0, ordinary=True),
                (True, True),
            )
        await self.store.set_reaction_mode(False)
        self.assertEqual(
            await self.store.add_message_with_reaction(USER, 33, 30, 133.0, ordinary=True),
            (True, False),
        )
        await self.store.set_reaction_mode(True)
        await self.store.delete_data(99, 210.0)
        self.assertFalse((await self.store.state())["reaction_mode"])

    async def test_admin_decisions_survive_restart_and_data_deletion(self) -> None:
        self.assertIsNone(await self.store.admin_override(31))
        await self.store.set_admin_override(31, True)
        await self.store.set_admin_override(32, False)
        self.assertEqual(await self.store.admin_overrides(), {31: True, 32: False})
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200.0, 11)
        self.assertEqual(await self.store.admin_overrides(), {31: True, 32: False})
        await self.store.delete_data(99, 210.0)
        self.assertEqual(await self.store.admin_overrides(), {31: True, 32: False})

    async def test_concurrent_duplicate_delivery_commits_only_once(self) -> None:
        stamp = epoch("2025-02-03T12:00:00")
        results = await asyncio.gather(
            *(self.store.add_message(USER, 17, 30, stamp) for _ in range(20))
        )
        self.assertEqual(sum(results), 1)
        self.assertEqual((await self.store.stats(USER, "all", stamp + 1))["messages"], 1)

    async def test_voice_move_splits_days_but_keeps_one_complete_visit(self) -> None:
        connected = epoch("2025-03-02T23:49:00")
        start = epoch("2025-03-02T23:50:00")
        move = epoch("2025-03-02T23:55:00")
        checkpoint = epoch("2025-03-03T00:05:00")
        end = epoch("2025-03-03T00:10:00")
        await self.store.connect(connected)
        await self.store.voice_transition(USER, 101, start)
        await self.store.voice_transition(USER, 102, move)
        await self.store.checkpoint(checkpoint)
        during = await self.store.stats(USER, "all", checkpoint)
        self.assertEqual(during["voice_visits"], 1)
        self.assertAlmostEqual(during["voice_seconds"], 900)
        self.assertEqual(during["active_days"], 2)
        await self.store.voice_transition(USER, None, end)

        result = await self.store.stats(USER, "all", end)
        self.assertAlmostEqual(result["voice_seconds"], 1200)
        self.assertEqual(result["voice_visits"], 1)
        self.assertEqual(result["active_days"], 2)
        record = await self.store.records(USER, end)
        self.assertAlmostEqual(record["longest_visit_seconds"], 1200)
        self.assertEqual(record["longest_visit_at"], start)

        monday_week = await self.store.stats(USER, "week", end)
        self.assertAlmostEqual(monday_week["voice_seconds"], 600)
        self.assertEqual(monday_week["active_days"], 1)
        self.assertAlmostEqual(
            sum(row["seconds"] for row in await self.store.company_totals(USER, "week", end)),
            600,
        )

    async def test_daily_trend_fills_every_day_and_splits_live_voice(self) -> None:
        await self.store.add_message(USER, 1, 30, epoch("2025-03-01T23:30:00"))
        await self.store.add_message(USER, 2, 30, epoch("2025-03-03T00:01:00"))
        await self.store.connect(epoch("2025-03-02T23:00:00"))
        await self.store.voice_transition(USER, 101, epoch("2025-03-02T23:50:00"))
        await self.store.checkpoint(epoch("2025-03-03T00:05:00"))
        now = epoch("2025-03-03T00:10:00")

        live = await self.store.daily_trend(USER, "month", now)
        self.assertEqual([row["day"] for row in live], ["2025-03-01", "2025-03-02", "2025-03-03"])
        self.assertEqual([row["messages"] for row in live], [1, 0, 1])
        self.assertEqual([round(row["voice_seconds"]) for row in live], [0, 600, 600])
        self.assertEqual(live[1]["voice_visits"], 1)

        saved = await self.store.daily_trend(USER, "month", now, include_live=False)
        self.assertEqual([round(row["voice_seconds"]) for row in saved], [0, 600, 300])
        later = await self.store.daily_trend(USER, "week", epoch("2025-03-03T09:00:00"), include_live=False)
        self.assertEqual([(row["day"], row["messages"]) for row in later], [("2025-03-03", 1)])

    async def test_message_times_and_voice_hours_use_local_time_and_retained_start(self) -> None:
        store = await self.new_store(
            "hours", epoch("2025-03-01T00:00:00"), "America/Costa_Rica"
        )
        try:
            await store.connect(epoch("2025-03-03T00:00:00"))
            await store.add_message(USER, 1, 30, epoch("2025-03-03T04:30:00"))  # 22:30 local
            await store.add_message(USER, 2, 30, epoch("2025-03-03T05:10:00"))  # 23:10 local
            await store.voice_transition(USER, 101, epoch("2025-03-03T04:30:00"))
            await store.voice_transition(USER, None, epoch("2025-03-03T06:00:00"))
            await store.voice_transition(USER, 101, epoch("2025-03-03T14:00:00"))  # 08:00 local
            await store.checkpoint(epoch("2025-03-03T14:10:00"))
            now = epoch("2025-03-03T14:30:00")

            times = await store.message_times(USER, "all", now)
            self.assertEqual(times["times"], [epoch("2025-03-03T04:30:00"), epoch("2025-03-03T05:10:00")])
            self.assertEqual(times["since"], times["period_start"])
            voice = await store.voice_hours(USER, "all", now)
            self.assertEqual((voice["hours"][22], voice["hours"][23], voice["hours"][8]), (1800.0, 3600.0, 1800.0))
            saved = await store.voice_hours(USER, "all", now, include_live=False)
            self.assertEqual(saved["hours"][8], 600.0)

            await store.voice_transition(USER, None, now)
            await store.maintenance(epoch("2025-03-03T05:00:00") + 86400, 1)
            later = epoch("2025-03-04T06:00:00")
            pruned = await store.message_times(USER, "all", later)
            self.assertEqual(pruned["times"], [epoch("2025-03-03T05:10:00")])
            self.assertGreater(pruned["since"], pruned["period_start"])
            self.assertEqual(sum((await store.voice_hours(USER, "all", later))["hours"]), 5400.0)
        finally:
            await store.close()

    async def test_daily_trend_marks_only_finished_fully_watched_days(self) -> None:
        await self.store.connect(epoch("2025-03-02T00:00:00"))
        await self.store.checkpoint(epoch("2025-03-04T06:00:00"))
        await self.store.disconnect(epoch("2025-03-04T06:00:30"))
        await self.store.connect(epoch("2025-03-04T07:00:00"))
        await self.store.checkpoint(epoch("2025-03-06T12:00:00"))
        rows = await self.store.daily_trend(USER, "month", epoch("2025-03-06T12:00:00"))
        self.assertEqual(
            [(row["day"], row["watched"]) for row in rows],
            [("2025-03-01", False), ("2025-03-02", True), ("2025-03-03", True),
             ("2025-03-04", False), ("2025-03-05", True), ("2025-03-06", False)],
        )

    async def test_daily_trend_starts_at_tracking_and_live_coverage_counts_through_now(self) -> None:
        store = await self.new_store("late", epoch("2025-03-24T12:00:00"))
        try:
            await store.connect(epoch("2025-03-24T12:00:00"))
            await store.checkpoint(epoch("2025-03-26T00:00:00"))
            rows = await store.daily_trend(USER, "month", epoch("2025-03-26T00:00:00"))
            self.assertEqual([row["day"] for row in rows], ["2025-03-24", "2025-03-25", "2025-03-26"])
            self.assertEqual([row["watched"] for row in rows], [False, True, False])

            now = epoch("2025-03-26T00:09:55")  # 9m55s after the last checkpoint
            live = await store.period_comparison(USER, "today", now)
            self.assertEqual(live["current"]["watched_seconds"], 595.0)
            saved = await store.period_comparison(USER, "today", now, include_live=False)
            self.assertEqual(saved["current"]["watched_seconds"], 0.0)
        finally:
            await store.close()

    async def test_retained_detail_start_ignores_deletion_reset(self) -> None:
        await self.store.delete_data(99, epoch("2025-03-05T12:00:00"))
        await self.store.set_paused(False, 99, epoch("2025-03-05T12:00:00"))
        await self.store.add_message(USER, 1, 30, epoch("2025-03-05T13:00:00"))
        result = await self.store.message_times(USER, "week", epoch("2025-03-05T14:00:00"))
        self.assertEqual(result["since"], result["period_start"])
        self.assertEqual(len(result["times"]), 1)

    async def test_period_comparison_uses_same_elapsed_window_and_refuses_unfair_cases(self) -> None:
        store = await self.new_store("cmp", epoch("2025-02-20T00:00:00"))
        try:
            await store.connect(epoch("2025-02-20T00:00:00"))
            await store.add_message(USER, 1, 30, epoch("2025-02-24T09:00:00"))  # last Monday, in window
            await store.add_message(USER, 2, 30, epoch("2025-02-25T09:00:00"))  # last Tuesday, after window
            await store.add_message(USER, 3, 30, epoch("2025-03-03T08:00:00"))
            await store.add_message(USER, 4, 30, epoch("2025-03-03T09:00:00"))
            await store.voice_transition(USER, 101, epoch("2025-02-24T11:00:00"))
            await store.voice_transition(USER, None, epoch("2025-02-24T13:00:00"))  # 1h inside window
            await store.voice_transition(USER, 101, epoch("2025-03-03T10:00:00"))
            await store.checkpoint(epoch("2025-03-03T11:00:00"))
            now = epoch("2025-03-03T12:00:00")

            result = await store.period_comparison(USER, "week", now)
            self.assertIsNone(result["reason"])
            current, previous = result["current"], result["previous"]
            self.assertEqual((previous["start"], previous["end"]), (epoch("2025-02-24T00:00:00"), epoch("2025-02-24T12:00:00")))
            self.assertEqual((current["messages"], previous["messages"]), (2, 1))
            self.assertEqual((current["voice_seconds"], previous["voice_seconds"]), (7200.0, 3600.0))
            self.assertEqual((current["voice_visits"], previous["voice_visits"]), (1, 1))
            self.assertEqual(previous["watched_seconds"], 12 * 3600.0)
            saved = await store.period_comparison(USER, "week", now, include_live=False)
            self.assertEqual(saved["current"]["voice_seconds"], 3600.0)

            self.assertEqual((await store.period_comparison(USER, "all", now))["reason"], "all")
            self.assertEqual((await store.period_comparison(USER, "month", now))["reason"], "untracked")
            await store.voice_transition(USER, None, now)
            await store.maintenance(now, 3)
            self.assertEqual((await store.period_comparison(USER, "week", now))["reason"], "pruned")
        finally:
            await store.close()

    async def test_company_daily_keeps_days_and_matches_company_totals(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 86_000, companions={31})
        await self.store.checkpoint(86_500)
        rows = await self.store.company_daily(USER, "all", 86_600)
        self.assertEqual(
            [(row["day"], row["member_id"], row["seconds"]) for row in rows],
            [("1970-01-01", 31, 400.0), ("1970-01-02", 31, 200.0)],
        )
        totals = await self.store.company_totals(USER, "all", 86_600)
        self.assertEqual(
            totals, [{"channel_id": 101, "member_id": 31, "seconds": 600.0, "full_seconds": 600.0}]
        )

    async def test_company_time_splits_peers_and_stops_at_disconnect_checkpoint(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110, companions={31, 32})
        await self.store.companion_transition(101, 32, False, 140)
        await self.store.companion_transition(999, 33, True, 150)
        await self.store.voice_transition(USER, 102, 160, companions=set())
        await self.store.checkpoint(175)
        live = await self.store.company_totals(USER, "all", 180)
        self.assertEqual(
            {(row["channel_id"], row["member_id"]): row["seconds"] for row in live},
            {(101, 31): 35.0, (101, 32): 15.0, (102, 0): 20.0},
        )
        await self.store.disconnect(200)
        saved = await self.store.company_totals(USER, "all", 220)
        self.assertEqual(sum(row["seconds"] for row in saved), 65.0)
        self.assertEqual((await self.store.stats(USER, "all", 220))["voice_seconds"], 65.0)
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(250, 11)
        self.assertEqual(await self.store.company_totals(USER, "all", 250), saved)
        await self.store.maintenance(1000 + 90 * 86400, 90)
        self.assertEqual(await self.store.company_totals(USER, "all", 1000 + 90 * 86400), saved)
        await self.store.delete_data(99, 1001 + 90 * 86400)
        self.assertEqual(await self.store.company_totals(USER, "all", 1002 + 90 * 86400), [])

    async def test_company_full_time_credits_every_peer_with_the_whole_time(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110, companions={31, 32, 33})
        await self.store.companion_transition(101, 33, False, 140)
        await self.store.checkpoint(170)
        live = await self.store.company_totals(USER, "all", 200)
        self.assertEqual(
            {row["member_id"]: (row["seconds"], row["full_seconds"]) for row in live},
            {31: (40.0, 90.0), 32: (40.0, 90.0), 33: (10.0, 30.0)},
        )
        await self.store.disconnect(200)
        saved = await self.store.company_totals(USER, "all", 220)
        self.assertEqual({row["member_id"]: row["full_seconds"] for row in saved}, {31: 60.0, 32: 60.0, 33: 30.0})

    async def test_company_recovery_discards_uncheckpointed_time(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110, companions={31})
        await self.store.checkpoint(120)
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200, 11)
        self.assertEqual((await self.store.company_totals(USER, "all", 200))[0]["seconds"], 10.0)
        await self.store.connect(250)
        await self.store.voice_transition(USER, 101, 250, complete_start=False, companions={32})
        await self.store.voice_transition(USER, None, 260)
        self.assertEqual(
            {row["member_id"]: row["seconds"] for row in await self.store.company_totals(USER, "all", 260)},
            {31: 10.0, 32: 10.0},
        )

    async def test_disconnect_counts_only_checkpointed_voice_and_gap_until_reconnect(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(200)
        await self.store.disconnect(300)

        offline = await self.store.stats(USER, "all", 350)
        self.assertAlmostEqual(offline["voice_seconds"], 90)
        self.assertAlmostEqual(offline["gap_seconds"], 150)

        await self.store.connect(400)
        # Gateway reconciliation sees the current channel, not its original join.
        await self.store.voice_transition(USER, 101, 400, complete_start=False)
        await self.store.voice_transition(USER, None, 500)
        result = await self.store.stats(USER, "all", 500)
        self.assertAlmostEqual(result["voice_seconds"], 190)
        self.assertAlmostEqual(result["gap_seconds"], 200)
        self.assertEqual(result["voice_visits"], 1)  # The reconciliation is not a join.
        self.assertEqual((await self.store.records(USER, 500))["longest_visit_at"], None)

    async def test_restart_closes_open_segment_at_checkpoint_and_keeps_gap_open(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(200)
        await self.store.close()  # Simulate an unclean process exit.

        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(300, 11)
        at_startup = await self.store.stats(USER, "all", 300)
        self.assertAlmostEqual(at_startup["voice_seconds"], 90)
        self.assertAlmostEqual(at_startup["gap_seconds"], 100)
        self.assertEqual((await self.store.records(USER, 300))["longest_visit_at"], None)

        await self.store.connect(400)
        await self.store.voice_transition(USER, 101, 400, complete_start=False)
        self.assertAlmostEqual((await self.store.stats(USER, "all", 450))["gap_seconds"], 200)

    async def test_brief_disconnect_continues_visit_without_counting_gap(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(170)
        await self.store.disconnect(175)
        await self.store.connect(200)
        await self.store.voice_transition(USER, 101, 200, complete_start=False)
        live = await self.store.records(USER, 260)
        self.assertAlmostEqual(live["current_visit_seconds"], 120)
        self.assertTrue(live["current_visit_complete_start"])
        await self.store.voice_transition(USER, None, 300)

        result = await self.store.stats(USER, "all", 300)
        self.assertAlmostEqual(result["voice_seconds"], 160)  # 30s gap is not counted.
        self.assertAlmostEqual(result["gap_seconds"], 30)
        self.assertEqual(result["voice_visits"], 1)
        record = await self.store.records(USER, 300)
        self.assertAlmostEqual(record["longest_visit_seconds"], 160)
        self.assertEqual(record["longest_visit_at"], 110)
        self.assertIsNone(record["current_visit_seconds"])

    async def test_restart_continues_visit_in_same_channel(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(200)
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(230, 11)
        await self.store.connect(240)
        await self.store.voice_transition(USER, 101, 240, complete_start=False)
        await self.store.voice_transition(USER, None, 300)
        record = await self.store.records(USER, 300)
        self.assertAlmostEqual(record["longest_visit_seconds"], 150)
        self.assertEqual(record["longest_visit_at"], 110)

    async def test_back_to_back_bridges_and_crash_recovery_keep_one_visit(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 100)
        await self.store.checkpoint(160)
        await self.store.disconnect(165)
        await self.store.connect(180)
        await self.store.voice_transition(USER, 101, 180, complete_start=False)
        await self.store.checkpoint(240)
        await self.store.close()  # Crash: no disconnect before restart.
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(250, 11)
        await self.store.connect(270)
        await self.store.voice_transition(USER, 101, 270, complete_start=False)
        await self.store.voice_transition(USER, None, 370)

        result = await self.store.stats(USER, "all", 370)
        self.assertAlmostEqual(result["voice_seconds"], 220)
        self.assertAlmostEqual(result["gap_seconds"], 50)
        self.assertEqual(result["voice_visits"], 1)
        record = await self.store.records(USER, 370)
        self.assertAlmostEqual(record["longest_visit_seconds"], 220)
        self.assertEqual(record["longest_visit_at"], 100)

    async def test_records_omit_live_visit_when_paused_or_unreliable(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(170)
        self.assertAlmostEqual((await self.store.records(USER, 200))["current_visit_seconds"], 90)
        self.assertIsNone(
            (await self.store.records(USER, 200, include_live=False))["current_visit_seconds"]
        )
        await self.store.set_paused(True, 99, 210)
        self.assertIsNone((await self.store.records(USER, 220))["current_visit_seconds"])

    async def test_long_outage_channel_change_or_pause_keeps_visit_split(self) -> None:
        bridge = Store.VISIT_BRIDGE_SECONDS
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(200)
        await self.store.disconnect(205)
        await self.store.connect(201 + bridge)
        await self.store.voice_transition(USER, 101, 201 + bridge, complete_start=False)
        await self.store.voice_transition(USER, None, 300 + bridge)
        self.assertIsNone((await self.store.records(USER, 300 + bridge))["longest_visit_at"])

        start = 1000
        await self.store.voice_transition(USER, 101, start)
        await self.store.checkpoint(start + 50)
        await self.store.disconnect(start + 55)
        await self.store.connect(start + 60)
        await self.store.voice_transition(USER, 102, start + 60, complete_start=False)
        await self.store.voice_transition(USER, None, start + 120)
        self.assertIsNone((await self.store.records(USER, start + 120))["longest_visit_at"])

        start = 2000
        await self.store.voice_transition(USER, 102, start)
        await self.store.checkpoint(start + 50)
        await self.store.set_paused(True, 99, start + 50)
        await self.store.set_paused(False, 99, start + 60)
        await self.store.connect(start + 60)
        await self.store.voice_transition(USER, 102, start + 60, complete_start=False)
        await self.store.voice_transition(USER, None, start + 120)
        self.assertIsNone((await self.store.records(USER, start + 120))["longest_visit_at"])

    async def test_startup_recomputes_record_from_visits_split_by_outages(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.voice_transition(USER, None, 140)  # 30s fully observed visit.
        await self.store.voice_transition(USER, 101, 200)
        await self.store.checkpoint(260)
        await self.store.disconnect(265)
        await self.store.connect(280)
        await self.store.voice_transition(USER, 101, 280, complete_start=False)
        await self.store.voice_transition(USER, None, 400)
        # Simulate visits split before bridging existed.
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE voice_visits SET ended_at = 260, complete_end = 0 WHERE visit_id = 2")
            conn.execute(
                "INSERT INTO voice_visits(user_id, started_at, ended_at, complete_start, complete_end, observed_seconds) "
                "VALUES ('22', 280, 400, 0, 1, 120)"
            )
            conn.execute("UPDATE voice_visits SET observed_seconds = 60 WHERE visit_id = 2")
            conn.execute("UPDATE voice_segments SET visit_id = 3 WHERE started_at = 280")
            conn.execute("UPDATE records SET value = 30, at = '110.0' "
                "WHERE user_id = '22' AND record_type = 'longest_visit'")
            conn.commit()
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(500, 11)
        record = await self.store.records(USER, 500)
        self.assertAlmostEqual(record["longest_visit_seconds"], 180)
        self.assertEqual(record["longest_visit_at"], 200)

    async def test_legacy_target_pause_can_be_cleared_by_admin_and_visit_is_incomplete(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(120)
        await self.store.set_paused(True, 22, 130)
        self.assertFalse(await self.store.add_message(USER, 8, 30, 135))
        await self.store.set_paused(False, 99, 140)
        self.assertFalse((await self.store.state())["paused"])
        self.assertEqual((await self.store.stats(USER, "all", 140))["voice_visits"], 1)
        self.assertEqual((await self.store.records(USER, 140))["longest_visit_at"], None)
        self.assertEqual((await self.store.stats(USER, "all", 140))["gap_seconds"], 0)

    async def test_maintenance_prunes_detail_but_retains_totals_and_seven_backups(self) -> None:
        old = epoch("2024-01-01T12:00:00")
        recent = epoch("2025-01-01T12:00:00")
        await self.store.add_message(USER, 101, 30, old)
        await self.store.add_message(USER, 102, 30, recent)
        now = epoch("2025-01-02T00:00:00")
        await self.store.maintenance(now, 90)
        self.assertFalse(await self.store.add_message(USER, 103, 30, old))
        self.assertEqual((await self.store.stats(USER, "all", now))["messages"], 2)

        for offset in range(1, 8):
            await self.store.maintenance(now + offset * 86400, 90)
        backups = sorted(self.backups.glob("flock-cctv-*.sqlite3"))
        self.assertEqual(len(backups), 7)
        with closing(sqlite3.connect(backups[-1])) as backup:
            self.assertEqual(backup.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 1)
            self.assertEqual(
                backup.execute("SELECT SUM(messages) FROM daily_stats").fetchone()[0], 2
            )

        unmanaged = self.backups / "keep-this.txt"
        unmanaged.write_text("unmanaged", encoding="utf-8")
        orphan_temp = self.backups / ".flock-cctv-2025-01-09.ABC123.sqlite3.tmp"
        orphan_temp.write_text("partial", encoding="utf-8")
        await self.store.delete_data(99, now + 8 * 86400)
        self.assertEqual(list(self.backups.glob("flock-cctv-*.sqlite3")), [])
        self.assertFalse(orphan_temp.exists())
        self.assertTrue(unmanaged.exists())
        state = await self.store.state()
        self.assertTrue(state["paused"])
        self.assertEqual(state["paused_by"], "99")
        self.assertEqual(state["tracking_since"], now + 8 * 86400)
        self.assertEqual((await self.store.stats(USER, "all", now + 8 * 86400))["messages"], 0)
        self.assertEqual(await self.store.records(USER, now + 8 * 86400), {
            "busiest_day": None,
            "busiest_day_messages": 0,
            "longest_visit_seconds": 0.0,
            "longest_visit_at": None,
            "current_visit_seconds": None,
            "current_visit_complete_start": False,
        })

    async def test_database_identity_and_timezone_are_fixed(self) -> None:
        with self.assertRaises(StoreError):
            await self.store.initialize(200, 99)
        other_timezone = Store(self.db, self.backups, "America/Costa_Rica")
        try:
            with self.assertRaises(StoreError):
                await other_timezone.initialize(200, 11)
        finally:
            await other_timezone.close()

    async def test_deletion_removes_documented_manual_backups_and_orphan_sidecars(self) -> None:
        await self.store.add_message(USER, 201, 30, 150)
        await self.store.maintenance(200, 90)
        daily = next(self.backups.glob("flock-cctv-*.sqlite3"))
        managed_names = [
            "flock-cctv-manual.sqlite3",
            "flock-cctv-before-restore.sqlite3",
            "flock-cctv-before-update.sqlite3",
            "flock-cctv-manual.sqlite3-wal",
            ".flock-cctv-1970-01-01.ABC123.sqlite3.tmp-journal",
        ]
        for name in managed_names:
            (self.backups / name).write_bytes(daily.read_bytes())
        unrelated = self.backups / "unrelated.sqlite3"
        unrelated.write_text("not managed")

        await self.store.delete_data(22, 210)
        self.assertEqual({path.name for path in self.backups.iterdir()}, {unrelated.name})
        self.assertEqual((await self.store.stats(USER, "all", 220))["messages"], 0)

    async def test_committed_deletion_succeeds_when_optional_compaction_fails(self) -> None:
        await self.store.add_message(USER, 201, 30, 150)
        await self.store.maintenance(200, 90)
        with patch.object(
            self.store, "_compact_after_deletion",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ), self.assertLogs("flock_cctv.storage", level="ERROR"):
            await self.store.delete_data(22, 210)
        self.assertTrue((await self.store.state())["paused"])
        self.assertEqual((await self.store.stats(USER, "all", 220))["messages"], 0)
        self.assertFalse(list(self.backups.iterdir()))

    async def test_backup_restores_totals_and_records_with_interrupted_voice(self) -> None:
        await self.store.connect(100)
        await self.store.add_message(USER, 201, 30, 150)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.voice_transition(USER, None, 140)
        await self.store.voice_transition(USER, 101, 150)
        await self.store.checkpoint(180)
        await self.store.maintenance(190, 90)
        snapshot = next(self.backups.glob("flock-cctv-*.sqlite3"))
        restore_path = self.root / "restored.sqlite3"
        with closing(sqlite3.connect(snapshot)) as source, closing(sqlite3.connect(restore_path)) as destination:
            source.backup(destination)
        restored = Store(restore_path, self.root / "restored-backups", "UTC")
        try:
            await restored.initialize(300, 11)
            stats = await restored.stats(USER, "all", 300)
            self.assertEqual(stats["messages"], 1)
            self.assertEqual(stats["voice_seconds"], 60)
            self.assertEqual(stats["gap_seconds"], 120)
            self.assertEqual((await restored.records(USER, 300))["longest_visit_seconds"], 30)
        finally:
            await restored.close()

    async def test_unreconciled_reports_can_disable_live_voice_extrapolation(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(150)
        self.assertEqual((await self.store.stats(USER, "all", 300))["voice_seconds"], 190)
        saved = await self.store.stats(USER, "all", 300, include_live=False)
        self.assertEqual(saved["voice_seconds"], 40)

    async def test_last_voice_follows_moves_leave_and_persists_past_detail_retention(self) -> None:
        self.assertIsNone(await self.store.last_voice(USER, 100))
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        self.assertEqual(await self.store.last_voice(USER, 120), {
            "channel_id": 101, "seen_at": 120, "current": True, "observed_since": 110,
        })
        await self.store.voice_transition(USER, 102, 130)
        self.assertEqual((await self.store.last_voice(USER, 140))["channel_id"], 102)
        await self.store.voice_transition(USER, None, 150)
        self.assertEqual(await self.store.last_voice(USER, 160), {
            "channel_id": 102, "seen_at": 150, "current": False, "observed_since": None,
        })
        await self.store.maintenance(150 + 100 * 86400, 90)
        self.assertEqual((await self.store.last_voice(USER, 150 + 100 * 86400))["seen_at"], 150)
        await self.store.delete_data(22, 150 + 100 * 86400 + 1)
        self.assertIsNone(await self.store.last_voice(USER, 150 + 100 * 86400 + 2))

    async def test_last_voice_does_not_claim_current_during_disconnect(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(150)
        self.assertEqual((await self.store.last_voice(USER, 300, include_live=False))["seen_at"], 150)
        await self.store.disconnect(300)
        self.assertEqual(await self.store.last_voice(USER, 400), {
            "channel_id": 101, "seen_at": 150, "current": False, "observed_since": None,
        })




class MultiPersonStoreTests(unittest.IsolatedAsyncioTestCase):
    """Several tracked people sharing one collector, coverage, and channel."""

    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "tracker.sqlite3"
        self.backups = self.root / "backups"
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(100.0, 11)
        for user in (USER, OTHER, THIRD):
            self.assertTrue(await self.store.track_user(user, 99, 100.0))

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    def rows(self, sql: str, *params: object) -> list[tuple]:
        with closing(sqlite3.connect(self.db)) as conn:
            return [tuple(row) for row in conn.execute(sql, params)]

    async def reopen(self, now: float) -> None:
        await self.store.close()  # Simulate an unclean process exit.
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(now, 11)

    async def test_fresh_database_uses_schema_one_and_tracks_nobody(self) -> None:
        store = Store(self.root / "fresh.sqlite3", self.root / "fresh-backups", "UTC")
        try:
            await store.initialize(100.0, 11)
            self.assertEqual(await store.tracked_users(), [])
            self.assertEqual(await store.active_user_ids(), frozenset())
            with closing(sqlite3.connect(self.root / "fresh.sqlite3")) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(Store.SCHEMA_VERSION, 1)
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertTrue({"tracked_users", "tracking_intervals", "records", "last_voice"} <= tables)
        finally:
            await store.close()

    async def test_newer_and_legacy_single_target_databases_are_refused(self) -> None:
        newer = self.root / "newer.sqlite3"
        with closing(sqlite3.connect(newer)) as conn:
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        legacy = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as conn:
            conn.execute(
                "CREATE TABLE settings (singleton INTEGER PRIMARY KEY, guild_id TEXT, "
                "target_user_id TEXT, timezone TEXT, tracking_since REAL, resume_boundary REAL)"
            )
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
        for path in (newer, legacy):
            store = Store(path, self.root / "refused-backups", "UTC")
            try:
                with self.assertRaises(StoreError):
                    await store.initialize(200.0, 11)
            finally:
                await store.close()

    async def test_database_identity_is_guild_and_timezone_only(self) -> None:
        with self.assertRaisesRegex(StoreError, "different guild"):
            await self.store.initialize(200, 99)
        other_timezone = Store(self.db, self.backups, "America/Costa_Rica")
        try:
            with self.assertRaisesRegex(StoreError, "timezone differs"):
                await other_timezone.initialize(200, 11)
        finally:
            await other_timezone.close()
        await self.store.initialize(200, 11)  # The same guild reopens cleanly.

    async def test_track_and_untrack_return_values_and_listing(self) -> None:
        self.assertFalse(await self.store.track_user(USER, 98, 150.0))  # Already active.
        self.assertFalse(await self.store.untrack_user(999, 98, 150.0))  # Never tracked.
        self.assertTrue(await self.store.untrack_user(THIRD, 98, 200.0))
        self.assertFalse(await self.store.untrack_user(THIRD, 98, 210.0))
        self.assertEqual(await self.store.active_user_ids(), frozenset({USER, OTHER}))
        self.assertTrue(await self.store.track_user(THIRD, 97, 300.0))
        self.assertTrue(await self.store.track_user(31, 97, 310.0))
        self.assertEqual(
            await self.store.tracked_users(),
            [
                {"user_id": 22, "active": True, "tracking_since": 100.0, "added_by": 99, "updated_at": 100.0},
                {"user_id": 23, "active": True, "tracking_since": 100.0, "added_by": 99, "updated_at": 100.0},
                # Re-tracking keeps the original tracking_since and records the latest add.
                {"user_id": 24, "active": True, "tracking_since": 100.0, "added_by": 97, "updated_at": 300.0},
                {"user_id": 31, "active": True, "tracking_since": 310.0, "added_by": 97, "updated_at": 310.0},
            ],
        )
        await self.store.untrack_user(31, 97, 400.0)
        listing = {row["user_id"]: row for row in await self.store.tracked_users()}
        self.assertFalse(listing[31]["active"])
        self.assertEqual(listing[31]["updated_at"], 400.0)
        # Tracking works while collection is paused; pause is handled by coverage.
        await self.store.set_paused(True, 99, 500.0)
        self.assertTrue(await self.store.track_user(40, 99, 510.0))
        self.assertIn(40, await self.store.active_user_ids())

    async def test_stats_report_tracked_and_known_flags_and_person_since(self) -> None:
        unknown = await self.store.stats(999, "all", 200)
        self.assertEqual((unknown["tracked"], unknown["known"], unknown["messages"]), (False, False, 0))
        self.assertTrue(await self.store.track_user(41, 99, 500.0))
        late = await self.store.stats(41, "all", 600)
        self.assertEqual((late["tracked"], late["known"]), (True, True))
        self.assertEqual(late["tracking_since"], 500.0)  # The person's own clock.
        self.assertEqual((await self.store.stats(USER, "all", 600))["tracking_since"], 100.0)
        await self.store.untrack_user(41, 99, 550.0)
        gone = await self.store.stats(41, "all", 600)
        self.assertEqual((gone["tracked"], gone["known"]), (False, True))
        self.assertEqual((await self.store.state())["tracking_since"], 100.0)

    async def test_messages_rejected_for_inactive_people_and_before_tracking_start(self) -> None:
        self.assertFalse(await self.store.add_message(999, 1, 30, 150.0))  # Never tracked.
        self.assertTrue(await self.store.track_user(50, 99, 500.0))
        self.assertFalse(await self.store.add_message(50, 2, 30, 499.0))  # Before their interval.
        self.assertTrue(await self.store.add_message(50, 3, 30, 500.0))
        self.assertEqual(
            await self.store.add_message_with_reaction(50, 4, 30, 400.0, ordinary=True), (False, False)
        )
        self.assertTrue(await self.store.untrack_user(50, 99, 600.0))
        self.assertFalse(await self.store.add_message(50, 5, 30, 650.0))
        self.assertEqual(
            await self.store.add_message_with_reaction(50, 6, 30, 650.0, ordinary=True), (False, False)
        )
        self.assertEqual((await self.store.stats(50, "all", 700))["messages"], 1)
        # A person tracked from the start still counts messages from the database start.
        self.assertFalse(await self.store.add_message(USER, 7, 30, 99.0))
        self.assertTrue(await self.store.add_message(USER, 8, 30, 100.0))
        # A re-track opens a new interval; the untracked stretch stays rejected.
        self.assertTrue(await self.store.track_user(50, 99, 800.0))
        self.assertFalse(await self.store.add_message(50, 9, 30, 700.0))
        self.assertTrue(await self.store.add_message(50, 10, 30, 800.0))

    async def test_voice_is_ignored_for_inactive_people_and_before_their_interval(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(999, 101, 110)
        self.assertIsNone(await self.store.last_voice(999, 120))
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_segments"), [(0,)])
        self.assertTrue(await self.store.track_user(51, 99, 500.0))
        await self.store.checkpoint(600)
        # A late event cannot start observing before the person was tracked.
        await self.store.voice_transition(51, 101, 400.0)
        self.assertEqual(
            self.rows("SELECT started_at FROM voice_segments WHERE user_id = '51'"), [(600.0,)]
        )
        await self.store.untrack_user(51, 99, 650.0)
        await self.store.voice_transition(51, 102, 660.0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_segments WHERE ended_at IS NULL"), [(0,)])

    async def test_messages_voice_records_and_last_voice_are_isolated_per_person(self) -> None:
        await self.store.connect(100)
        self.assertTrue(await self.store.add_message(USER, 1, 30, 150))
        self.assertTrue(await self.store.add_message(OTHER, 2, 30, 151))
        self.assertTrue(await self.store.add_message(OTHER, 3, 31, 152))
        self.assertFalse(await self.store.add_message(OTHER, 3, 31, 152))  # Duplicate ID.
        self.assertEqual((await self.store.stats(USER, "all", 160))["messages"], 1)
        self.assertEqual((await self.store.stats(OTHER, "all", 160))["messages"], 2)
        self.assertEqual((await self.store.stats(THIRD, "all", 160))["messages"], 0)
        self.assertEqual((await self.store.records(USER, 160))["busiest_day_messages"], 1)
        self.assertEqual((await self.store.records(OTHER, 160))["busiest_day_messages"], 2)

        await self.store.voice_transition(USER, 101, 110)
        self.assertIsNone(await self.store.last_voice(OTHER, 115))
        await self.store.voice_transition(OTHER, 102, 120)
        await self.store.checkpoint(200)
        self.assertEqual((await self.store.stats(USER, "all", 200))["voice_seconds"], 90)
        self.assertEqual((await self.store.stats(OTHER, "all", 200))["voice_seconds"], 80)
        self.assertEqual((await self.store.stats(THIRD, "all", 200))["voice_seconds"], 0)
        await self.store.voice_transition(OTHER, None, 210)
        # Only OTHER's visit completed, so only OTHER holds a longest-visit record.
        self.assertEqual((await self.store.records(OTHER, 250))["longest_visit_seconds"], 90)
        self.assertEqual((await self.store.records(OTHER, 250))["longest_visit_at"], 120)
        self.assertEqual((await self.store.records(USER, 250))["longest_visit_seconds"], 0.0)
        live = await self.store.records(USER, 250)
        self.assertAlmostEqual(live["current_visit_seconds"], 140)
        self.assertIsNone((await self.store.records(OTHER, 250))["current_visit_seconds"])
        self.assertEqual(await self.store.last_voice(USER, 250), {
            "channel_id": 101, "seen_at": 250, "current": True, "observed_since": 110,
        })
        self.assertEqual(await self.store.last_voice(OTHER, 250), {
            "channel_id": 102, "seen_at": 210, "current": False, "observed_since": None,
        })
        self.assertIsNone(await self.store.last_voice(THIRD, 250))
        self.assertEqual(
            await self.store.company_totals(USER, "all", 250),
            [{"channel_id": 101, "member_id": 0, "seconds": 140.0, "full_seconds": 140.0}],
        )
        self.assertEqual(
            await self.store.company_totals(OTHER, "all", 250),
            [{"channel_id": 102, "member_id": 0, "seconds": 90.0, "full_seconds": 90.0}],
        )
        hours = await self.store.voice_hours(OTHER, "all", 250)
        self.assertEqual(sum(hours["hours"]), 90.0)
        self.assertEqual((await self.store.message_times(USER, "all", 250))["times"], [150.0])
        self.assertEqual((await self.store.message_times(OTHER, "all", 250))["times"], [151.0, 152.0])
        trend = await self.store.daily_trend(OTHER, "all", 250)
        self.assertEqual((trend[0]["messages"], round(trend[0]["voice_seconds"])), (2, 90))

    async def test_two_tracked_people_in_one_channel_are_companions_with_untracked_people(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110, companions={OTHER, 31})
        await self.store.voice_transition(OTHER, 101, 110, companions={USER, 31})
        await self.store.checkpoint(170)
        self.assertEqual(
            await self.store.company_totals(USER, "all", 170),
            [
                {"channel_id": 101, "member_id": 23, "seconds": 30.0, "full_seconds": 60.0},
                {"channel_id": 101, "member_id": 31, "seconds": 30.0, "full_seconds": 60.0},
            ],
        )
        self.assertEqual(
            await self.store.company_totals(OTHER, "all", 170),
            [
                {"channel_id": 101, "member_id": 22, "seconds": 30.0, "full_seconds": 60.0},
                {"channel_id": 101, "member_id": 31, "seconds": 30.0, "full_seconds": 60.0},
            ],
        )
        # A person is never their own companion.
        await self.store.voice_transition(THIRD, 101, 170, companions={THIRD, USER})
        self.assertEqual(
            [json.loads(row[0]) for row in self.rows(
                "SELECT member_ids FROM voice_company_current WHERE user_id = '24'"
            )],
            [["22"]],
        )

    async def test_companion_transition_updates_every_roster_in_channel_and_skips_own_segment(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110, companions={OTHER, 31})
        await self.store.voice_transition(OTHER, 101, 110, companions={USER, 31})
        await self.store.voice_transition(THIRD, 102, 110)
        await self.store.companion_transition(101, 32, True, 130)
        await self.store.companion_transition(999, 33, True, 135)  # Nobody is there.
        # OTHER leaves channel 101: USER's roster loses OTHER; OTHER's own is untouched.
        await self.store.companion_transition(101, OTHER, False, 150)
        await self.store.checkpoint(170)

        def roster(user: int) -> list[str]:
            row = self.rows("SELECT member_ids FROM voice_company_current WHERE user_id = ?", str(user))
            return json.loads(row[0][0])

        self.assertEqual(roster(USER), ["31", "32"])
        self.assertEqual(roster(OTHER), ["22", "31", "32"])
        self.assertEqual(roster(THIRD), [])

        def totals(rows: list[dict]) -> dict[int, tuple[float, float]]:
            return {row["member_id"]: (row["seconds"], row["full_seconds"]) for row in rows}

        user = totals(await self.store.company_totals(USER, "all", 170))
        self.assertEqual(set(user), {23, 31, 32})
        self.assertAlmostEqual(user[23][0], 10 + 20 / 3)
        self.assertAlmostEqual(user[31][0], 10 + 20 / 3 + 10)
        self.assertAlmostEqual(user[32][0], 20 / 3 + 10)
        self.assertEqual((user[23][1], user[31][1], user[32][1]), (40.0, 60.0, 40.0))
        other = totals(await self.store.company_totals(OTHER, "all", 170))
        self.assertAlmostEqual(other[22][0], 10 + 40 / 3)
        self.assertAlmostEqual(other[32][0], 40 / 3)
        self.assertEqual((other[22][1], other[31][1], other[32][1]), (60.0, 60.0, 40.0))
        self.assertEqual(
            totals(await self.store.company_totals(THIRD, "all", 170)), {0: (60.0, 60.0)}
        )
        # Companion events never change what each person's own visit measured.
        for person in (USER, OTHER, THIRD):
            self.assertEqual((await self.store.stats(person, "all", 170))["voice_seconds"], 60)

    async def test_companion_transition_is_ignored_while_paused_or_disconnected(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.disconnect(120)
        await self.store.companion_transition(101, 31, True, 130)
        await self.store.connect(200)
        await self.store.companion_transition(101, 31, True, 210)  # Roster was cleared.
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_company_current"), [(0,)])

    async def test_each_person_has_at_most_one_open_segment(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.voice_transition(USER, 101, 120)  # Same channel: no second segment.
        await self.store.voice_transition(OTHER, 101, 120)
        await self.store.voice_transition(USER, 102, 130)  # A move closes the old one first.
        self.assertEqual(
            self.rows(
                "SELECT user_id, COUNT(*) FROM voice_segments WHERE ended_at IS NULL "
                "GROUP BY user_id ORDER BY user_id"
            ),
            [("22", 1), ("23", 1)],
        )
        with closing(sqlite3.connect(self.db)) as conn, self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO voice_segments(visit_id, user_id, channel_id, started_at, checkpoint) "
                "VALUES (1, '22', '101', 1, 1)"
            )

    async def test_checkpoint_advances_every_open_segment_to_one_point(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.voice_transition(THIRD, 102, 110)
        await self.store.companion_transition(101, 31, True, 150)  # Only USER's segment moves.
        self.assertEqual((await self.store.state())["last_checkpoint"], 150)
        await self.store.checkpoint(160)
        self.assertEqual((await self.store.stats(USER, "all", 160))["voice_seconds"], 50)
        self.assertEqual((await self.store.stats(THIRD, "all", 160))["voice_seconds"], 50)
        self.assertEqual(
            self.rows("SELECT DISTINCT checkpoint FROM voice_segments WHERE ended_at IS NULL"), [(160.0,)]
        )
        # The checkpoint never moves back, even for an event stamped earlier.
        await self.store.checkpoint(155)
        self.assertEqual((await self.store.state())["last_checkpoint"], 160)

    async def test_disconnect_closes_every_segment_at_its_own_checkpoint(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110, companions={OTHER})
        await self.store.voice_transition(OTHER, 102, 120, companions={USER})
        await self.store.checkpoint(200)
        await self.store.disconnect(300)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_segments WHERE ended_at IS NULL"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_company_current"), [(0,)])
        self.assertEqual((await self.store.stats(USER, "all", 350))["voice_seconds"], 90)
        self.assertEqual((await self.store.stats(OTHER, "all", 350))["voice_seconds"], 80)
        self.assertEqual((await self.store.stats(USER, "all", 350))["gap_seconds"], 150)
        self.assertEqual((await self.store.stats(THIRD, "all", 350))["gap_seconds"], 150)
        self.assertEqual(await self.store.last_voice(OTHER, 400), {
            "channel_id": 102, "seen_at": 200, "current": False, "observed_since": None,
        })
        self.assertEqual((await self.store.state())["last_checkpoint"], 200)

    async def test_disconnect_gap_starts_at_the_oldest_open_checkpoint(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.voice_transition(THIRD, 102, 110)
        await self.store.companion_transition(101, 31, True, 150)  # USER 150, THIRD 110.
        await self.store.disconnect(300)
        user = await self.store.stats(USER, "all", 300)
        third = await self.store.stats(THIRD, "all", 300)
        self.assertEqual((user["voice_seconds"], user["gap_seconds"]), (40, 190))
        self.assertEqual((third["voice_seconds"], third["gap_seconds"]), (0, 190))
        self.assertEqual((await self.store.state())["last_checkpoint"], 110)

    async def test_pause_closes_every_open_segment_as_incomplete(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110, companions={OTHER})
        await self.store.voice_transition(OTHER, 101, 120, companions={USER})
        await self.store.set_paused(True, 99, 150)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_segments WHERE ended_at IS NULL"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_company_current"), [(0,)])
        self.assertEqual((await self.store.stats(USER, "all", 160))["voice_seconds"], 40)
        self.assertEqual((await self.store.stats(OTHER, "all", 160))["voice_seconds"], 30)
        self.assertEqual((await self.store.records(USER, 160))["longest_visit_at"], None)
        self.assertEqual((await self.store.records(OTHER, 160))["longest_visit_at"], None)
        self.assertIsNone((await self.store.records(USER, 160))["current_visit_seconds"])

    async def test_recovery_closes_every_open_segment_and_bridges_each_person_separately(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110, companions={OTHER})
        await self.store.voice_transition(OTHER, 101, 110, companions={USER})
        await self.store.voice_transition(THIRD, 102, 110)
        await self.store.companion_transition(101, 31, True, 150)  # USER/OTHER 150, THIRD 110.
        await self.reopen(180)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_segments WHERE ended_at IS NULL"), [(0,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_company_current"), [(0,)])
        self.assertEqual(
            self.rows("SELECT user_id, ended_at, complete_end FROM voice_visits ORDER BY user_id"),
            [("22", 150.0, 0), ("23", 150.0, 0), ("24", 110.0, 0)],
        )
        # The restart gap begins at the oldest recovery point and stays open.
        user = await self.store.stats(USER, "all", 180)
        self.assertEqual((user["voice_seconds"], user["gap_seconds"]), (40, 70))
        self.assertEqual((await self.store.stats(OTHER, "all", 180))["voice_seconds"], 40)

        await self.store.connect(200)
        await self.store.voice_transition(USER, 101, 200, complete_start=False)
        await self.store.voice_transition(THIRD, 101, 200, complete_start=False)
        await self.store.voice_transition(USER, None, 260)
        await self.store.voice_transition(THIRD, None, 260)
        # USER continues their own visit; THIRD's earlier visit was elsewhere.
        record = await self.store.records(USER, 260)
        self.assertAlmostEqual(record["longest_visit_seconds"], 100)
        self.assertEqual(record["longest_visit_at"], 110)
        self.assertEqual((await self.store.stats(USER, "all", 260))["voice_visits"], 1)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_visits WHERE user_id = '24'"), [(2,)])
        self.assertIsNone((await self.store.records(THIRD, 260))["longest_visit_at"])
        self.assertIsNone((await self.store.records(OTHER, 260))["longest_visit_at"])

    async def test_startup_recomputes_each_persons_record_separately(self) -> None:
        await self.store.connect(100)
        for user in (USER, OTHER):
            await self.store.voice_transition(user, 101, 200)
        await self.store.checkpoint(260)
        await self.store.disconnect(265)
        await self.store.connect(280)
        await self.store.voice_transition(USER, 101, 280, complete_start=False)
        await self.store.voice_transition(OTHER, 102, 280, complete_start=False)
        await self.store.voice_transition(USER, None, 400)
        await self.store.voice_transition(OTHER, None, 400)
        # Drop the stored records and rebuild them at startup.
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM records WHERE record_type = 'longest_visit'")
            conn.commit()
        await self.reopen(500)
        # USER's visit continued across the outage; OTHER changed channels and did not.
        self.assertAlmostEqual((await self.store.records(USER, 500))["longest_visit_seconds"], 180)
        self.assertEqual((await self.store.records(USER, 500))["longest_visit_at"], 200)
        self.assertIsNone((await self.store.records(OTHER, 500))["longest_visit_at"])

    async def test_untrack_mid_visit_closes_visit_incomplete_and_keeps_history(self) -> None:
        await self.store.connect(100)
        self.assertTrue(await self.store.add_message(USER, 1, 30, 120))
        await self.store.voice_transition(USER, 101, 110, companions={OTHER})
        await self.store.voice_transition(OTHER, 101, 110, companions={USER})
        await self.store.checkpoint(150)
        self.assertTrue(await self.store.untrack_user(USER, 99, 170))

        self.assertEqual(self.rows("SELECT user_id FROM voice_segments WHERE ended_at IS NULL"), [("23",)])
        self.assertEqual(self.rows("SELECT user_id FROM voice_company_current"), [("23",)])
        result = await self.store.stats(USER, "all", 180)
        self.assertEqual((result["voice_seconds"], result["voice_visits"], result["messages"]), (60, 1, 1))
        self.assertEqual((result["tracked"], result["known"]), (False, True))
        record = await self.store.records(USER, 180)
        self.assertIsNone(record["longest_visit_at"])  # Incomplete end: not a record.
        self.assertIsNone(record["current_visit_seconds"])
        self.assertEqual(await self.store.last_voice(USER, 180), {
            "channel_id": 101, "seen_at": 170, "current": False, "observed_since": None,
        })
        self.assertEqual(
            await self.store.company_totals(USER, "all", 180),
            [{"channel_id": 101, "member_id": 23, "seconds": 60.0, "full_seconds": 60.0}],
        )
        self.assertEqual(
            self.rows("SELECT ended_at, complete_end FROM voice_visits WHERE user_id = '22'"),
            [(170.0, 0)],
        )
        self.assertEqual(
            self.rows("SELECT started_at, ended_at FROM tracking_intervals WHERE user_id = '22'"),
            [(100.0, 170.0)],
        )
        # Nothing more is collected for them, but OTHER is unaffected and still live.
        await self.store.voice_transition(USER, 102, 175)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_segments WHERE user_id = '22'"), [(1,)])
        self.assertAlmostEqual((await self.store.records(OTHER, 180))["current_visit_seconds"], 70)

    async def test_retracking_never_bridges_a_visit_across_an_untracked_stretch(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(150)
        self.assertTrue(await self.store.untrack_user(USER, 99, 170))
        await self.store.disconnect(180)
        await self.store.connect(190)
        self.assertTrue(await self.store.track_user(USER, 99, 190))
        # Same channel, inside the bridge window, across a disconnect gap: the
        # tracked list change is what keeps the visits apart.
        await self.store.voice_transition(USER, 101, 190, complete_start=False)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM voice_visits WHERE user_id = '22'"), [(2,)])
        self.assertEqual(
            self.rows("SELECT ended_at FROM voice_visits WHERE user_id = '22' ORDER BY visit_id"),
            [(170.0,), (None,)],
        )

    async def test_untracked_stretch_is_missing_coverage_and_never_a_quiet_day(self) -> None:
        start = epoch("2025-03-01T00:00:00")
        store = Store(self.root / "stretch.sqlite3", self.root / "stretch-backups", "UTC")
        await store.initialize(start, 11)
        try:
            await store.track_user(USER, 98, start)
            await store.track_user(OTHER, 98, start)
            await store.connect(start)
            await store.checkpoint(epoch("2025-03-03T12:00:00"))
            self.assertTrue(await store.untrack_user(USER, 98, epoch("2025-03-03T12:00:00")))
            self.assertFalse(await store.add_message(USER, 1, 30, epoch("2025-03-04T12:00:00")))
            await store.checkpoint(epoch("2025-03-05T12:00:00"))
            self.assertTrue(await store.track_user(USER, 97, epoch("2025-03-05T12:00:00")))
            self.assertFalse(await store.add_message(USER, 2, 30, epoch("2025-03-05T11:00:00")))
            self.assertTrue(await store.add_message(USER, 3, 30, epoch("2025-03-05T13:00:00")))
            now = epoch("2025-03-06T12:00:00")
            await store.checkpoint(now)

            rows = await store.daily_trend(USER, "month", now)
            self.assertEqual(
                [(row["day"], row["watched"]) for row in rows],
                [("2025-03-01", True), ("2025-03-02", True), ("2025-03-03", False),
                 ("2025-03-04", False), ("2025-03-05", False), ("2025-03-06", False)],
            )
            self.assertEqual([row["messages"] for row in rows], [0, 0, 0, 0, 1, 0])
            steady = await store.daily_trend(OTHER, "month", now)
            self.assertEqual([row["watched"] for row in steady], [True, True, True, True, True, False])

            result = await store.stats(USER, "all", now)
            self.assertEqual(result["gap_seconds"], 48 * 3600.0)
            self.assertEqual(result["tracking_since"], start)
            self.assertEqual(result["messages"], 1)
            self.assertEqual((await store.stats(OTHER, "all", now))["gap_seconds"], 0.0)
            week = await store.stats(USER, "week", now)  # Monday 03-03 onward.
            self.assertEqual(week["gap_seconds"], 48 * 3600.0)
            compare = await store.period_comparison(USER, "week", now)
            # Monday 12h before the untrack plus the day after the re-track.
            self.assertEqual(compare["current"]["watched_seconds"], 36 * 3600.0)
            self.assertEqual(
                self.rows_in(store, "SELECT started_at, ended_at FROM tracking_intervals "
                             "WHERE user_id = '22' ORDER BY interval_id"),
                [(start, epoch("2025-03-03T12:00:00")), (epoch("2025-03-05T12:00:00"), None)],
            )
            listing = {row["user_id"]: row for row in await store.tracked_users()}
            self.assertEqual(listing[USER]["tracking_since"], start)
            self.assertEqual(listing[USER]["added_by"], 97)
        finally:
            await store.close()

    def rows_in(self, store: Store, sql: str) -> list[tuple]:
        with closing(sqlite3.connect(store.path)) as conn:
            return [tuple(row) for row in conn.execute(sql)]

    async def test_gap_seconds_do_not_double_count_global_gaps_inside_untracked_time(self) -> None:
        await self.store.connect(1000)
        await self.store.checkpoint(1100)
        await self.store.disconnect(1100)
        self.assertTrue(await self.store.untrack_user(USER, 99, 1200))
        await self.store.connect(1500)
        self.assertTrue(await self.store.track_user(USER, 99, 1600))
        await self.store.checkpoint(1700)
        # Tracked 100-1200 and 1600-: recorded gap 1100-1500 overlaps tracking by
        # 100s; 1200-1600 is untracked. Together they cover 1100-1600 once.
        result = await self.store.stats(USER, "all", 1700)
        self.assertEqual(result["gap_seconds"], 500.0)
        self.assertEqual((await self.store.stats(OTHER, "all", 1700))["gap_seconds"], 400.0)
        compare = await self.store.period_comparison(USER, "all", 1700)
        # Coverage 100-1100 (connected at 1000) and 1500-1700, within tracking.
        self.assertEqual(compare["current"]["watched_seconds"], 100.0 + 100.0)

    async def test_gaps_before_a_person_was_tracked_are_not_theirs(self) -> None:
        await self.store.connect(100)
        await self.store.checkpoint(200)
        await self.store.disconnect(250)
        await self.store.connect(300)
        self.assertTrue(await self.store.track_user(60, 99, 500))
        await self.store.checkpoint(600)
        self.assertEqual((await self.store.stats(60, "all", 600))["gap_seconds"], 0.0)
        self.assertEqual((await self.store.stats(USER, "all", 600))["gap_seconds"], 100.0)

    async def test_period_comparison_untracked_reason_uses_person_since(self) -> None:
        store = Store(self.root / "cmp.sqlite3", self.root / "cmp-backups", "UTC")
        await store.initialize(epoch("2025-02-20T00:00:00"), 11)
        try:
            await store.track_user(USER, 98, epoch("2025-02-20T00:00:00"))
            await store.connect(epoch("2025-02-20T00:00:00"))
            await store.track_user(OTHER, 98, epoch("2025-03-03T00:00:00"))
            now = epoch("2025-03-05T12:00:00")  # Wednesday
            await store.checkpoint(now)
            veteran = await store.period_comparison(USER, "week", now)
            self.assertIsNone(veteran["reason"])
            self.assertEqual(veteran["previous"]["watched_seconds"], 60 * 3600.0)
            newcomer = await store.period_comparison(OTHER, "week", now)
            self.assertEqual((newcomer["previous"], newcomer["reason"]), (None, "untracked"))
            self.assertEqual(newcomer["current"]["watched_seconds"], 60 * 3600.0)
            self.assertEqual((await store.period_comparison(OTHER, "all", now))["reason"], "all")
            self.assertEqual((await store.period_comparison(USER, "month", now))["reason"], "untracked")
        finally:
            await store.close()

    async def test_live_time_is_per_person_and_follows_connection_and_pause(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        await self.store.checkpoint(150)
        self.assertEqual((await self.store.stats(USER, "all", 200))["voice_seconds"], 90)
        self.assertEqual((await self.store.stats(USER, "all", 200, include_live=False))["voice_seconds"], 40)
        self.assertEqual((await self.store.stats(OTHER, "all", 200))["voice_seconds"], 0)
        self.assertTrue((await self.store.last_voice(USER, 200))["current"])
        self.assertIsNone(await self.store.last_voice(OTHER, 200))
        self.assertFalse((await self.store.last_voice(USER, 200, include_live=False))["current"])
        self.assertEqual(
            (await self.store.company_totals(USER, "all", 200, include_live=False))[0]["seconds"], 40
        )

    async def test_per_person_delete_leaves_others_untouched_and_keeps_companion_rows(self) -> None:
        await self.store.connect(100)
        await self.store.add_message(USER, 1, 30, 150)
        await self.store.add_message(OTHER, 2, 30, 151)
        await self.store.voice_transition(USER, 101, 110, companions={OTHER})
        await self.store.voice_transition(OTHER, 101, 110, companions={USER})
        await self.store.checkpoint(200)
        await self.store.maintenance(250, 90)
        managed = self.backups / "flock-cctv-manual.sqlite3"
        managed.write_bytes(next(self.backups.glob("flock-cctv-*.sqlite3")).read_bytes())
        unmanaged = self.backups / "keep-this.txt"
        unmanaged.write_text("unmanaged", encoding="utf-8")
        await self.store.set_evil_mode(True)

        self.assertTrue(await self.store.delete_user_data(USER, 99, 300))
        self.assertEqual(list(self.backups.glob("flock-cctv-*.sqlite3")), [])
        self.assertTrue(unmanaged.exists())
        state = await self.store.state()
        self.assertFalse(state["paused"])  # Global collection continues.
        self.assertTrue(state["evil_mode"])  # Legacy modes only reset when asked.
        gone = await self.store.stats(USER, "all", 300)
        self.assertEqual((gone["messages"], gone["voice_seconds"], gone["known"]), (0, 0, False))
        self.assertEqual([row["user_id"] for row in await self.store.tracked_users()], [OTHER, THIRD])
        self.assertIsNone(await self.store.last_voice(USER, 300))
        for table in (
            "messages", "daily_stats", "voice_visits", "voice_segments", "voice_company_current",
            "voice_company_daily", "last_voice", "records", "tracking_intervals", "tracked_users",
        ):
            self.assertEqual(
                self.rows(f"SELECT COUNT(*) FROM {table} WHERE user_id = '22'"), [(0,)], table
            )
        # OTHER's statistics, including the deleted person as their companion, stay.
        self.assertEqual(self.rows(
            "SELECT COUNT(*) FROM voice_company_daily WHERE user_id = '23' AND member_id = '22'"
        ), [(1,)])
        other = await self.store.stats(OTHER, "all", 300)
        self.assertEqual((other["messages"], other["voice_seconds"]), (1, 190))
        self.assertEqual(
            await self.store.company_totals(OTHER, "all", 300),
            [{"channel_id": 101, "member_id": 22, "seconds": 190.0, "full_seconds": 190.0}],
        )
        # The other people are still collected; the deleted person is not tracked any more.
        self.assertTrue(await self.store.add_message(OTHER, 5, 30, 310))
        self.assertFalse(await self.store.add_message(USER, 6, 30, 310))
        await self.store.checkpoint(320)
        self.assertEqual((await self.store.stats(OTHER, "all", 320))["voice_seconds"], 210)

        # Nothing to delete: False, other people's backups survive, modes stay.
        await self.store.maintenance(330, 90)
        self.assertFalse(await self.store.delete_user_data(999, 99, 331))
        self.assertEqual(len(list(self.backups.glob("flock-cctv-*.sqlite3"))), 1)
        self.assertTrue((await self.store.state())["evil_mode"])

        await self.store.set_reaction_mode(True)
        self.assertTrue(await self.store.delete_user_data(OTHER, 99, 340, reset_legacy_modes=True))
        state = await self.store.state()
        self.assertEqual((state["evil_mode"], state["reaction_mode"], state["paused"]), (False, False, False))
        self.assertEqual([row["user_id"] for row in await self.store.tracked_users()], [THIRD])
        self.assertEqual(await self.store.admin_overrides(), {})

    async def test_per_person_delete_of_a_former_person_with_history_only(self) -> None:
        await self.store.connect(100)
        await self.store.add_message(THIRD, 1, 30, 150)
        await self.store.untrack_user(THIRD, 99, 160)
        self.assertEqual((await self.store.stats(THIRD, "all", 170))["messages"], 1)
        self.assertTrue(await self.store.delete_user_data(THIRD, 99, 170))
        self.assertFalse((await self.store.stats(THIRD, "all", 180))["known"])
        self.assertFalse(await self.store.delete_user_data(THIRD, 99, 190))

    async def test_global_delete_keeps_active_tracked_people_with_a_fresh_interval(self) -> None:
        await self.store.connect(100)
        for index, user in enumerate((USER, OTHER, THIRD)):
            await self.store.add_message(user, index + 1, 30, 150)
        await self.store.voice_transition(OTHER, 101, 120)
        await self.store.untrack_user(THIRD, 98, 160)
        await self.store.set_admin_override(31, True)

        await self.store.delete_data(99, 1000)
        self.assertEqual(
            await self.store.tracked_users(),
            [
                {"user_id": 22, "active": True, "tracking_since": 1000.0, "added_by": 99, "updated_at": 1000.0},
                {"user_id": 23, "active": True, "tracking_since": 1000.0, "added_by": 99, "updated_at": 1000.0},
            ],
        )
        self.assertEqual(
            self.rows("SELECT user_id, started_at, ended_at FROM tracking_intervals ORDER BY user_id"),
            [("22", 1000.0, None), ("23", 1000.0, None)],
        )
        self.assertEqual(await self.store.admin_overrides(), {31: True})
        for table in ("messages", "daily_stats", "voice_visits", "voice_segments", "records", "last_voice"):
            self.assertEqual(self.rows(f"SELECT COUNT(*) FROM {table}"), [(0,)], table)
        state = await self.store.state()
        self.assertEqual((state["paused"], state["tracking_since"]), (True, 1000.0))
        reset = await self.store.stats(USER, "all", 1000)
        self.assertEqual((reset["tracking_since"], reset["tracked"], reset["messages"]), (1000.0, True, 0))
        self.assertFalse((await self.store.stats(THIRD, "all", 1000))["known"])

        await self.store.set_paused(False, 99, 1100)
        await self.store.connect(1100)
        self.assertTrue(await self.store.add_message(USER, 9, 30, 1100))
        self.assertFalse(await self.store.add_message(THIRD, 10, 30, 1100))
        self.assertFalse(await self.store.add_message(USER, 11, 30, 999))
        self.assertEqual((await self.store.stats(USER, "all", 1200))["gap_seconds"], 0.0)
        self.assertTrue(await self.store.track_user(THIRD, 99, 1150))
        self.assertEqual((await self.store.stats(THIRD, "all", 1200))["tracking_since"], 1150.0)

    async def test_ranking_includes_live_voice_and_formerly_tracked_people_with_activity(self) -> None:
        self.assertTrue(await self.store.track_user(25, 99, 100.0))
        self.assertTrue(await self.store.track_user(26, 99, 100.0))
        await self.store.connect(100)
        for message_id in (1, 2, 3):
            await self.store.add_message(USER, message_id, 30, 120 + message_id)
        await self.store.add_message(OTHER, 4, 30, 125)
        await self.store.add_message(25, 5, 30, 120)
        await self.store.untrack_user(25, 99, 130)  # Formerly tracked, with activity.
        await self.store.untrack_user(26, 99, 105)  # Formerly tracked, no activity.
        await self.store.voice_transition(USER, 101, 110)
        await self.store.voice_transition(OTHER, 102, 120)
        await self.store.voice_transition(OTHER, None, 140)
        await self.store.checkpoint(150)

        rows = {row["user_id"]: row for row in await self.store.ranking("all", 200)}
        self.assertEqual(sorted(rows), [22, 23, 24, 25])
        self.assertEqual(
            rows[22],
            {"user_id": 22, "messages": 3, "voice_seconds": 90.0, "voice_visits": 1,
             "active_days": 1, "tracked": True},
        )
        self.assertEqual(
            rows[23],
            {"user_id": 23, "messages": 1, "voice_seconds": 20.0, "voice_visits": 1,
             "active_days": 1, "tracked": True},
        )
        self.assertEqual(
            rows[24],
            {"user_id": 24, "messages": 0, "voice_seconds": 0.0, "voice_visits": 0,
             "active_days": 0, "tracked": True},
        )
        self.assertEqual(
            rows[25],
            {"user_id": 25, "messages": 1, "voice_seconds": 0.0, "voice_visits": 0,
             "active_days": 1, "tracked": False},
        )
        self.assertEqual(
            [row["user_id"] for row in await self.store.ranking("all", 200)], [22, 23, 24, 25]
        )
        saved = {row["user_id"]: row for row in await self.store.ranking("all", 200, include_live=False)}
        self.assertEqual(saved[22]["voice_seconds"], 40.0)
        # A later period has no activity: only the people still tracked remain.
        later = await self.store.ranking("today", 3 * 86400, include_live=False)
        self.assertEqual([(row["user_id"], row["messages"], row["tracked"]) for row in later],
                         [(22, 0, True), (23, 0, True), (24, 0, True)])
        for row in await self.store.ranking("all", 200):
            stats = await self.store.stats(row["user_id"], "all", 200)
            self.assertEqual(
                (row["messages"], row["voice_seconds"], row["voice_visits"], row["active_days"]),
                (stats["messages"], stats["voice_seconds"], stats["voice_visits"], stats["active_days"]),
            )

    async def test_ranking_live_days_count_as_active(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(USER, 101, 110)
        live = {row["user_id"]: row for row in await self.store.ranking("today", 500)}
        self.assertEqual((live[22]["voice_seconds"], live[22]["active_days"]), (390.0, 1))
        self.assertEqual((live[23]["voice_seconds"], live[23]["active_days"]), (0.0, 0))
        await self.store.set_paused(True, 99, 600)
        paused = {row["user_id"]: row for row in await self.store.ranking("today", 700)}
        self.assertEqual(paused[22]["voice_seconds"], 490.0)  # Closed at the pause, not extrapolated.

if __name__ == "__main__":
    unittest.main()
