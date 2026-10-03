from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flock_cctv.storage import Store, StoreError


def epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "tracker.sqlite3"
        self.backups = self.root / "backups"
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(100.0, 11, 22)

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.temp.cleanup()

    async def test_messages_deduplicate_update_days_and_enforce_pause_boundary(self) -> None:
        stamp = epoch("2025-02-03T12:00:00")
        self.assertTrue(await self.store.add_message(1, 30, stamp))
        self.assertFalse(await self.store.add_message(1, 30, stamp))
        self.assertTrue(await self.store.add_message(2, 30, stamp + 1))
        self.assertEqual((await self.store.stats("all", stamp + 2))["messages"], 2)
        self.assertEqual(await self.store.records(stamp + 2), {
            "busiest_day": "2025-02-03",
            "busiest_day_messages": 2,
            "longest_visit_seconds": 0.0,
            "longest_visit_at": None,
            "current_visit_seconds": None,
            "current_visit_complete_start": False,
        })

        await self.store.set_paused(True, 99, stamp + 3)
        self.assertFalse(await self.store.add_message(3, 30, stamp + 4))
        await self.store.set_paused(False, 99, stamp + 10)
        self.assertFalse(await self.store.add_message(4, 30, stamp + 9))
        self.assertTrue(await self.store.add_message(5, 30, stamp + 10))
        self.assertEqual((await self.store.stats("all", stamp + 11))["messages"], 3)

    async def test_evil_mode_persists_and_deletion_disables_it(self) -> None:
        self.assertFalse((await self.store.state())["evil_mode"])
        await self.store.set_evil_mode(True)
        self.assertTrue((await self.store.state())["evil_mode"])
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200.0, 11, 22)
        self.assertTrue((await self.store.state())["evil_mode"])
        await self.store.delete_data(99, 210.0)
        self.assertFalse((await self.store.state())["evil_mode"])

    async def test_reaction_mode_counts_only_when_enabled_and_survives_restart(self) -> None:
        self.assertFalse((await self.store.state())["reaction_mode"])
        self.assertEqual(
            await self.store.add_message_with_reaction(1, 30, 101.0, ordinary=True),
            (True, False),
        )
        with patch("flock_cctv.storage.random.randint", return_value=15):
            await self.store.set_reaction_mode(True)
            self.assertEqual(
                await self.store.add_message_with_reaction(2, 30, 102.0, ordinary=False),
                (True, False),
            )
            for message_id in range(3, 10):
                self.assertEqual(
                    await self.store.add_message_with_reaction(message_id, 30, float(message_id + 100), ordinary=True),
                    (True, False),
                )
            await self.store.close()
            self.store = Store(self.db, self.backups, "UTC")
            await self.store.initialize(200.0, 11, 22)
            self.assertTrue((await self.store.state())["reaction_mode"])
            self.assertEqual(
                await self.store.add_message_with_reaction(9, 30, 109.0, ordinary=True),
                (False, False),
            )
            for message_id in range(10, 17):
                self.assertEqual(
                    await self.store.add_message_with_reaction(message_id, 30, float(message_id + 100), ordinary=True),
                    (True, False),
                )
            # A crash after this commit but before Discord accepts a reaction
            # cannot make the replay decrement the next interval twice.
            self.assertEqual(
                await self.store.add_message_with_reaction(17, 30, 117.0, ordinary=True),
                (True, True),
            )
            await self.store.close()
            self.store = Store(self.db, self.backups, "UTC")
            await self.store.initialize(220.0, 11, 22)
            self.assertEqual(
                await self.store.add_message_with_reaction(17, 30, 117.0, ordinary=True),
                (False, False),
            )
            for message_id in range(18, 32):
                self.assertEqual(
                    await self.store.add_message_with_reaction(message_id, 30, float(message_id + 100), ordinary=True),
                    (True, False),
                )
            self.assertEqual(
                await self.store.add_message_with_reaction(32, 30, 132.0, ordinary=True),
                (True, True),
            )
        await self.store.set_reaction_mode(False)
        self.assertEqual(
            await self.store.add_message_with_reaction(33, 30, 133.0, ordinary=True),
            (True, False),
        )
        await self.store.set_reaction_mode(True)
        await self.store.delete_data(99, 210.0)
        self.assertFalse((await self.store.state())["reaction_mode"])

    async def test_schema_two_upgrades_with_evil_mode_off(self) -> None:
        await self.store.close()
        with sqlite3.connect(self.db) as connection:
            connection.execute("ALTER TABLE settings DROP COLUMN evil_mode")
            connection.execute("PRAGMA user_version = 2")
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200.0, 11, 22)
        self.assertFalse((await self.store.state())["evil_mode"])
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
        self.assertFalse((await self.store.state())["reaction_mode"])

    async def test_admin_decisions_survive_restart_and_data_deletion(self) -> None:
        self.assertIsNone(await self.store.admin_override(31))
        await self.store.set_admin_override(31, True)
        await self.store.set_admin_override(32, False)
        self.assertEqual(await self.store.admin_overrides(), {31: True, 32: False})
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200.0, 11, 22)
        self.assertEqual(await self.store.admin_overrides(), {31: True, 32: False})
        await self.store.delete_data(99, 210.0)
        self.assertEqual(await self.store.admin_overrides(), {31: True, 32: False})

    async def test_concurrent_duplicate_delivery_commits_only_once(self) -> None:
        stamp = epoch("2025-02-03T12:00:00")
        results = await asyncio.gather(
            *(self.store.add_message(17, 30, stamp) for _ in range(20))
        )
        self.assertEqual(sum(results), 1)
        self.assertEqual((await self.store.stats("all", stamp + 1))["messages"], 1)

    async def test_voice_move_splits_days_but_keeps_one_complete_visit(self) -> None:
        connected = epoch("2025-03-02T23:49:00")
        start = epoch("2025-03-02T23:50:00")
        move = epoch("2025-03-02T23:55:00")
        checkpoint = epoch("2025-03-03T00:05:00")
        end = epoch("2025-03-03T00:10:00")
        await self.store.connect(connected)
        await self.store.voice_transition(101, start)
        await self.store.voice_transition(102, move)
        await self.store.checkpoint(checkpoint)
        during = await self.store.stats("all", checkpoint)
        self.assertEqual(during["voice_visits"], 1)
        self.assertAlmostEqual(during["voice_seconds"], 900)
        self.assertEqual(during["active_days"], 2)
        await self.store.voice_transition(None, end)

        result = await self.store.stats("all", end)
        self.assertAlmostEqual(result["voice_seconds"], 1200)
        self.assertEqual(result["voice_visits"], 1)
        self.assertEqual(result["active_days"], 2)
        record = await self.store.records(end)
        self.assertAlmostEqual(record["longest_visit_seconds"], 1200)
        self.assertEqual(record["longest_visit_at"], start)

        monday_week = await self.store.stats("week", end)
        self.assertAlmostEqual(monday_week["voice_seconds"], 600)
        self.assertEqual(monday_week["active_days"], 1)
        self.assertAlmostEqual(
            sum(row["seconds"] for row in await self.store.company_totals("week", end)),
            600,
        )

    async def test_daily_trend_fills_every_day_and_splits_live_voice(self) -> None:
        await self.store.add_message(1, 30, epoch("2025-03-01T23:30:00"))
        await self.store.add_message(2, 30, epoch("2025-03-03T00:01:00"))
        await self.store.connect(epoch("2025-03-02T23:00:00"))
        await self.store.voice_transition(101, epoch("2025-03-02T23:50:00"))
        await self.store.checkpoint(epoch("2025-03-03T00:05:00"))
        now = epoch("2025-03-03T00:10:00")

        live = await self.store.daily_trend("month", now)
        self.assertEqual([row["day"] for row in live], ["2025-03-01", "2025-03-02", "2025-03-03"])
        self.assertEqual([row["messages"] for row in live], [1, 0, 1])
        self.assertEqual([round(row["voice_seconds"]) for row in live], [0, 600, 600])
        self.assertEqual(live[1]["voice_visits"], 1)

        saved = await self.store.daily_trend("month", now, include_live=False)
        self.assertEqual([round(row["voice_seconds"]) for row in saved], [0, 600, 300])
        later = await self.store.daily_trend("week", epoch("2025-03-03T09:00:00"), include_live=False)
        self.assertEqual([(row["day"], row["messages"]) for row in later], [("2025-03-03", 1)])

    async def test_message_times_and_voice_hours_use_local_time_and_retained_start(self) -> None:
        store = Store(self.root / "hours.sqlite3", self.root / "hour-backups", "America/Costa_Rica")
        await store.initialize(epoch("2025-03-01T00:00:00"), 11, 22)
        try:
            await store.connect(epoch("2025-03-03T00:00:00"))
            await store.add_message(1, 30, epoch("2025-03-03T04:30:00"))  # 22:30 local
            await store.add_message(2, 30, epoch("2025-03-03T05:10:00"))  # 23:10 local
            await store.voice_transition(101, epoch("2025-03-03T04:30:00"))
            await store.voice_transition(None, epoch("2025-03-03T06:00:00"))
            await store.voice_transition(101, epoch("2025-03-03T14:00:00"))  # 08:00 local
            await store.checkpoint(epoch("2025-03-03T14:10:00"))
            now = epoch("2025-03-03T14:30:00")

            times = await store.message_times("all", now)
            self.assertEqual(times["times"], [epoch("2025-03-03T04:30:00"), epoch("2025-03-03T05:10:00")])
            self.assertEqual(times["since"], times["period_start"])
            voice = await store.voice_hours("all", now)
            self.assertEqual((voice["hours"][22], voice["hours"][23], voice["hours"][8]), (1800.0, 3600.0, 1800.0))
            saved = await store.voice_hours("all", now, include_live=False)
            self.assertEqual(saved["hours"][8], 600.0)

            await store.voice_transition(None, now)
            await store.maintenance(epoch("2025-03-03T05:00:00") + 86400, 1)
            later = epoch("2025-03-04T06:00:00")
            pruned = await store.message_times("all", later)
            self.assertEqual(pruned["times"], [epoch("2025-03-03T05:10:00")])
            self.assertGreater(pruned["since"], pruned["period_start"])
            self.assertEqual(sum((await store.voice_hours("all", later))["hours"]), 5400.0)
        finally:
            await store.close()

    async def test_daily_trend_marks_only_finished_fully_watched_days(self) -> None:
        await self.store.connect(epoch("2025-03-02T00:00:00"))
        await self.store.checkpoint(epoch("2025-03-04T06:00:00"))
        await self.store.disconnect(epoch("2025-03-04T06:00:30"))
        await self.store.connect(epoch("2025-03-04T07:00:00"))
        await self.store.checkpoint(epoch("2025-03-06T12:00:00"))
        rows = await self.store.daily_trend("month", epoch("2025-03-06T12:00:00"))
        self.assertEqual(
            [(row["day"], row["watched"]) for row in rows],
            [("2025-03-01", False), ("2025-03-02", True), ("2025-03-03", True),
             ("2025-03-04", False), ("2025-03-05", True), ("2025-03-06", False)],
        )

    async def test_daily_trend_starts_at_tracking_and_live_coverage_counts_through_now(self) -> None:
        store = Store(self.root / "late.sqlite3", self.root / "late-backups", "UTC")
        await store.initialize(epoch("2025-03-24T12:00:00"), 11, 22)
        try:
            await store.connect(epoch("2025-03-24T12:00:00"))
            await store.checkpoint(epoch("2025-03-26T00:00:00"))
            rows = await store.daily_trend("month", epoch("2025-03-26T00:00:00"))
            self.assertEqual([row["day"] for row in rows], ["2025-03-24", "2025-03-25", "2025-03-26"])
            self.assertEqual([row["watched"] for row in rows], [False, True, False])

            now = epoch("2025-03-26T00:09:55")  # 9m55s after the last checkpoint
            live = await store.period_comparison("today", now)
            self.assertEqual(live["current"]["watched_seconds"], 595.0)
            saved = await store.period_comparison("today", now, include_live=False)
            self.assertEqual(saved["current"]["watched_seconds"], 0.0)
        finally:
            await store.close()

    async def test_retained_detail_start_ignores_deletion_reset(self) -> None:
        await self.store.delete_data(99, epoch("2025-03-05T12:00:00"))
        await self.store.set_paused(False, 99, epoch("2025-03-05T12:00:00"))
        await self.store.add_message(1, 30, epoch("2025-03-05T13:00:00"))
        result = await self.store.message_times("week", epoch("2025-03-05T14:00:00"))
        self.assertEqual(result["since"], result["period_start"])
        self.assertEqual(len(result["times"]), 1)

    async def test_period_comparison_uses_same_elapsed_window_and_refuses_unfair_cases(self) -> None:
        store = Store(self.root / "cmp.sqlite3", self.root / "cmp-backups", "UTC")
        await store.initialize(epoch("2025-02-20T00:00:00"), 11, 22)
        try:
            await store.connect(epoch("2025-02-20T00:00:00"))
            await store.add_message(1, 30, epoch("2025-02-24T09:00:00"))  # last Monday, in window
            await store.add_message(2, 30, epoch("2025-02-25T09:00:00"))  # last Tuesday, after window
            await store.add_message(3, 30, epoch("2025-03-03T08:00:00"))
            await store.add_message(4, 30, epoch("2025-03-03T09:00:00"))
            await store.voice_transition(101, epoch("2025-02-24T11:00:00"))
            await store.voice_transition(None, epoch("2025-02-24T13:00:00"))  # 1h inside window
            await store.voice_transition(101, epoch("2025-03-03T10:00:00"))
            await store.checkpoint(epoch("2025-03-03T11:00:00"))
            now = epoch("2025-03-03T12:00:00")

            result = await store.period_comparison("week", now)
            self.assertIsNone(result["reason"])
            current, previous = result["current"], result["previous"]
            self.assertEqual((previous["start"], previous["end"]), (epoch("2025-02-24T00:00:00"), epoch("2025-02-24T12:00:00")))
            self.assertEqual((current["messages"], previous["messages"]), (2, 1))
            self.assertEqual((current["voice_seconds"], previous["voice_seconds"]), (7200.0, 3600.0))
            self.assertEqual((current["voice_visits"], previous["voice_visits"]), (1, 1))
            self.assertEqual(previous["watched_seconds"], 12 * 3600.0)
            saved = await store.period_comparison("week", now, include_live=False)
            self.assertEqual(saved["current"]["voice_seconds"], 3600.0)

            self.assertEqual((await store.period_comparison("all", now))["reason"], "all")
            self.assertEqual((await store.period_comparison("month", now))["reason"], "untracked")
            await store.voice_transition(None, now)
            await store.maintenance(now, 3)
            self.assertEqual((await store.period_comparison("week", now))["reason"], "pruned")
        finally:
            await store.close()

    async def test_company_daily_keeps_days_and_matches_company_totals(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 86_000, companions={31})
        await self.store.checkpoint(86_500)
        rows = await self.store.company_daily("all", 86_600)
        self.assertEqual(
            [(row["day"], row["member_id"], row["seconds"]) for row in rows],
            [("1970-01-01", 31, 400.0), ("1970-01-02", 31, 200.0)],
        )
        totals = await self.store.company_totals("all", 86_600)
        self.assertEqual(
            totals, [{"channel_id": 101, "member_id": 31, "seconds": 600.0, "full_seconds": 600.0}]
        )

    async def test_company_time_splits_peers_and_stops_at_disconnect_checkpoint(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110, companions={31, 32})
        await self.store.companion_transition(101, 32, False, 140)
        await self.store.companion_transition(999, 33, True, 150)
        await self.store.voice_transition(102, 160, companions=set())
        await self.store.checkpoint(175)
        live = await self.store.company_totals("all", 180)
        self.assertEqual(
            {(row["channel_id"], row["member_id"]): row["seconds"] for row in live},
            {(101, 31): 35.0, (101, 32): 15.0, (102, 0): 20.0},
        )
        await self.store.disconnect(200)
        saved = await self.store.company_totals("all", 220)
        self.assertEqual(sum(row["seconds"] for row in saved), 65.0)
        self.assertEqual((await self.store.stats("all", 220))["voice_seconds"], 65.0)
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(250, 11, 22)
        self.assertEqual(await self.store.company_totals("all", 250), saved)
        await self.store.maintenance(1000 + 90 * 86400, 90)
        self.assertEqual(await self.store.company_totals("all", 1000 + 90 * 86400), saved)
        await self.store.delete_data(99, 1001 + 90 * 86400)
        self.assertEqual(await self.store.company_totals("all", 1002 + 90 * 86400), [])

    async def test_company_full_time_credits_every_peer_with_the_whole_time(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110, companions={31, 32, 33})
        await self.store.companion_transition(101, 33, False, 140)
        await self.store.checkpoint(170)
        live = await self.store.company_totals("all", 200)
        self.assertEqual(
            {row["member_id"]: (row["seconds"], row["full_seconds"]) for row in live},
            {31: (40.0, 90.0), 32: (40.0, 90.0), 33: (10.0, 30.0)},
        )
        await self.store.disconnect(200)
        saved = await self.store.company_totals("all", 220)
        self.assertEqual({row["member_id"]: row["full_seconds"] for row in saved}, {31: 60.0, 32: 60.0, 33: 30.0})

    async def test_schema_six_upgrade_counts_earlier_company_time_as_its_split_share(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110, companions={31, 32})
        await self.store.checkpoint(130)
        await self.store.disconnect(130)
        await self.store.close()
        with sqlite3.connect(self.db) as connection:
            connection.execute("ALTER TABLE voice_company_daily DROP COLUMN full_seconds")
            connection.execute("PRAGMA user_version = 6")
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200.0, 11, 22)
        self.assertEqual(
            {row["member_id"]: (row["seconds"], row["full_seconds"])
             for row in await self.store.company_totals("all", 200)},
            {31: (10.0, 10.0), 32: (10.0, 10.0)},
        )
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)

    async def test_schema_seven_and_eight_upgrades_raise_whole_time_to_the_split(self) -> None:
        # 31: only pre-schema-7 time. 32: whole time already above the split.
        # 33: an upgrade day whose earlier split time outweighs its later whole time.
        rows = [("31", 20.0, 0.0), ("32", 15.0, 30.0), ("33", 4800.0, 900.0)]
        for version in (7, 8):
            await self.store.close()
            with sqlite3.connect(self.db) as connection:
                connection.execute("DELETE FROM voice_company_daily")
                connection.executemany(
                    "INSERT INTO voice_company_daily(day, channel_id, member_id, seconds, full_seconds) "
                    "VALUES ('1970-01-01', '101', ?, ?, ?)",
                    rows,
                )
                connection.execute(f"PRAGMA user_version = {version}")
            self.store = Store(self.db, self.backups, "UTC")
            await self.store.initialize(200.0, 11, 22)
            self.assertEqual(
                {row["member_id"]: row["full_seconds"]
                 for row in await self.store.company_totals("all", 200)},
                {31: 20.0, 32: 30.0, 33: 4800.0},
            )

    async def test_company_recovery_discards_uncheckpointed_time(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110, companions={31})
        await self.store.checkpoint(120)
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200, 11, 22)
        self.assertEqual((await self.store.company_totals("all", 200))[0]["seconds"], 10.0)
        await self.store.connect(250)
        await self.store.voice_transition(101, 250, complete_start=False, companions={32})
        await self.store.voice_transition(None, 260)
        self.assertEqual(
            {row["member_id"]: row["seconds"] for row in await self.store.company_totals("all", 260)},
            {31: 10.0, 32: 10.0},
        )

    async def test_disconnect_counts_only_checkpointed_voice_and_gap_until_reconnect(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(200)
        await self.store.disconnect(300)

        offline = await self.store.stats("all", 350)
        self.assertAlmostEqual(offline["voice_seconds"], 90)
        self.assertAlmostEqual(offline["gap_seconds"], 150)

        await self.store.connect(400)
        # Gateway reconciliation sees the current channel, not its original join.
        await self.store.voice_transition(101, 400, complete_start=False)
        await self.store.voice_transition(None, 500)
        result = await self.store.stats("all", 500)
        self.assertAlmostEqual(result["voice_seconds"], 190)
        self.assertAlmostEqual(result["gap_seconds"], 200)
        self.assertEqual(result["voice_visits"], 1)  # The reconciliation is not a join.
        self.assertEqual((await self.store.records(500))["longest_visit_at"], None)

    async def test_restart_closes_open_segment_at_checkpoint_and_keeps_gap_open(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(200)
        await self.store.close()  # Simulate an unclean process exit.

        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(300, 11, 22)
        at_startup = await self.store.stats("all", 300)
        self.assertAlmostEqual(at_startup["voice_seconds"], 90)
        self.assertAlmostEqual(at_startup["gap_seconds"], 100)
        self.assertEqual((await self.store.records(300))["longest_visit_at"], None)

        await self.store.connect(400)
        await self.store.voice_transition(101, 400, complete_start=False)
        self.assertAlmostEqual((await self.store.stats("all", 450))["gap_seconds"], 200)

    async def test_brief_disconnect_continues_visit_without_counting_gap(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(170)
        await self.store.disconnect(175)
        await self.store.connect(200)
        await self.store.voice_transition(101, 200, complete_start=False)
        live = await self.store.records(260)
        self.assertAlmostEqual(live["current_visit_seconds"], 120)
        self.assertTrue(live["current_visit_complete_start"])
        await self.store.voice_transition(None, 300)

        result = await self.store.stats("all", 300)
        self.assertAlmostEqual(result["voice_seconds"], 160)  # 30s gap is not counted.
        self.assertAlmostEqual(result["gap_seconds"], 30)
        self.assertEqual(result["voice_visits"], 1)
        record = await self.store.records(300)
        self.assertAlmostEqual(record["longest_visit_seconds"], 160)
        self.assertEqual(record["longest_visit_at"], 110)
        self.assertIsNone(record["current_visit_seconds"])

    async def test_restart_continues_visit_in_same_channel(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(200)
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(230, 11, 22)
        await self.store.connect(240)
        await self.store.voice_transition(101, 240, complete_start=False)
        await self.store.voice_transition(None, 300)
        record = await self.store.records(300)
        self.assertAlmostEqual(record["longest_visit_seconds"], 150)
        self.assertEqual(record["longest_visit_at"], 110)

    async def test_back_to_back_bridges_and_crash_recovery_keep_one_visit(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 100)
        await self.store.checkpoint(160)
        await self.store.disconnect(165)
        await self.store.connect(180)
        await self.store.voice_transition(101, 180, complete_start=False)
        await self.store.checkpoint(240)
        await self.store.close()  # Crash: no disconnect before restart.
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(250, 11, 22)
        await self.store.connect(270)
        await self.store.voice_transition(101, 270, complete_start=False)
        await self.store.voice_transition(None, 370)

        result = await self.store.stats("all", 370)
        self.assertAlmostEqual(result["voice_seconds"], 220)
        self.assertAlmostEqual(result["gap_seconds"], 50)
        self.assertEqual(result["voice_visits"], 1)
        record = await self.store.records(370)
        self.assertAlmostEqual(record["longest_visit_seconds"], 220)
        self.assertEqual(record["longest_visit_at"], 100)

    async def test_records_omit_live_visit_when_paused_or_unreliable(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(170)
        self.assertAlmostEqual((await self.store.records(200))["current_visit_seconds"], 90)
        self.assertIsNone(
            (await self.store.records(200, include_live=False))["current_visit_seconds"]
        )
        await self.store.set_paused(True, 99, 210)
        self.assertIsNone((await self.store.records(220))["current_visit_seconds"])

    async def test_long_outage_channel_change_or_pause_keeps_visit_split(self) -> None:
        bridge = Store.VISIT_BRIDGE_SECONDS
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(200)
        await self.store.disconnect(205)
        await self.store.connect(201 + bridge)
        await self.store.voice_transition(101, 201 + bridge, complete_start=False)
        await self.store.voice_transition(None, 300 + bridge)
        self.assertIsNone((await self.store.records(300 + bridge))["longest_visit_at"])

        start = 1000
        await self.store.voice_transition(101, start)
        await self.store.checkpoint(start + 50)
        await self.store.disconnect(start + 55)
        await self.store.connect(start + 60)
        await self.store.voice_transition(102, start + 60, complete_start=False)
        await self.store.voice_transition(None, start + 120)
        self.assertIsNone((await self.store.records(start + 120))["longest_visit_at"])

        start = 2000
        await self.store.voice_transition(102, start)
        await self.store.checkpoint(start + 50)
        await self.store.set_paused(True, 99, start + 50)
        await self.store.set_paused(False, 99, start + 60)
        await self.store.connect(start + 60)
        await self.store.voice_transition(102, start + 60, complete_start=False)
        await self.store.voice_transition(None, start + 120)
        self.assertIsNone((await self.store.records(start + 120))["longest_visit_at"])

    async def test_startup_recomputes_record_from_visits_split_by_outages(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.voice_transition(None, 140)  # 30s fully observed visit.
        await self.store.voice_transition(101, 200)
        await self.store.checkpoint(260)
        await self.store.disconnect(265)
        await self.store.connect(280)
        await self.store.voice_transition(101, 280, complete_start=False)
        await self.store.voice_transition(None, 400)
        # Simulate visits split before bridging existed.
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE voice_visits SET ended_at = 260, complete_end = 0 WHERE visit_id = 2")
            conn.execute(
                "INSERT INTO voice_visits(started_at, ended_at, complete_start, complete_end, observed_seconds) "
                "VALUES (280, 400, 0, 1, 120)"
            )
            conn.execute("UPDATE voice_visits SET observed_seconds = 60 WHERE visit_id = 2")
            conn.execute("UPDATE voice_segments SET visit_id = 3 WHERE started_at = 280")
            conn.execute("UPDATE records SET value = 30, at = '110.0' WHERE record_type = 'longest_visit'")
            conn.commit()
        await self.store.close()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(500, 11, 22)
        record = await self.store.records(500)
        self.assertAlmostEqual(record["longest_visit_seconds"], 180)
        self.assertEqual(record["longest_visit_at"], 200)

    async def test_legacy_target_pause_can_be_cleared_by_admin_and_visit_is_incomplete(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(120)
        await self.store.set_paused(True, 22, 130)
        self.assertFalse(await self.store.add_message(8, 30, 135))
        await self.store.set_paused(False, 99, 140)
        self.assertFalse((await self.store.state())["paused"])
        self.assertEqual((await self.store.stats("all", 140))["voice_visits"], 1)
        self.assertEqual((await self.store.records(140))["longest_visit_at"], None)
        self.assertEqual((await self.store.stats("all", 140))["gap_seconds"], 0)

    async def test_maintenance_prunes_detail_but_retains_totals_and_seven_backups(self) -> None:
        old = epoch("2024-01-01T12:00:00")
        recent = epoch("2025-01-01T12:00:00")
        await self.store.add_message(101, 30, old)
        await self.store.add_message(102, 30, recent)
        now = epoch("2025-01-02T00:00:00")
        await self.store.maintenance(now, 90)
        self.assertFalse(await self.store.add_message(103, 30, old))
        self.assertEqual((await self.store.stats("all", now))["messages"], 2)

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
        self.assertEqual((await self.store.stats("all", now + 8 * 86400))["messages"], 0)
        self.assertEqual(await self.store.records(now + 8 * 86400), {
            "busiest_day": None,
            "busiest_day_messages": 0,
            "longest_visit_seconds": 0.0,
            "longest_visit_at": None,
            "current_visit_seconds": None,
            "current_visit_complete_start": False,
        })

    async def test_database_identity_and_timezone_are_fixed(self) -> None:
        with self.assertRaises(StoreError):
            await self.store.initialize(200, 99, 22)
        other_timezone = Store(self.db, self.backups, "America/Costa_Rica")
        try:
            with self.assertRaises(StoreError):
                await other_timezone.initialize(200, 11, 22)
        finally:
            await other_timezone.close()

    async def test_deletion_removes_documented_manual_backups_and_orphan_sidecars(self) -> None:
        await self.store.add_message(201, 30, 150)
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
        self.assertEqual((await self.store.stats("all", 220))["messages"], 0)

    async def test_committed_deletion_succeeds_when_optional_compaction_fails(self) -> None:
        await self.store.add_message(201, 30, 150)
        await self.store.maintenance(200, 90)
        with patch.object(
            self.store, "_compact_after_deletion",
            side_effect=sqlite3.OperationalError("database or disk is full"),
        ), self.assertLogs("flock_cctv.storage", level="ERROR"):
            await self.store.delete_data(22, 210)
        self.assertTrue((await self.store.state())["paused"])
        self.assertEqual((await self.store.stats("all", 220))["messages"], 0)
        self.assertFalse(list(self.backups.iterdir()))

    async def test_backup_restores_totals_and_records_with_interrupted_voice(self) -> None:
        await self.store.connect(100)
        await self.store.add_message(201, 30, 150)
        await self.store.voice_transition(101, 110)
        await self.store.voice_transition(None, 140)
        await self.store.voice_transition(101, 150)
        await self.store.checkpoint(180)
        await self.store.maintenance(190, 90)
        snapshot = next(self.backups.glob("flock-cctv-*.sqlite3"))
        restore_path = self.root / "restored.sqlite3"
        with closing(sqlite3.connect(snapshot)) as source, closing(sqlite3.connect(restore_path)) as destination:
            source.backup(destination)
        restored = Store(restore_path, self.root / "restored-backups", "UTC")
        try:
            await restored.initialize(300, 11, 22)
            stats = await restored.stats("all", 300)
            self.assertEqual(stats["messages"], 1)
            self.assertEqual(stats["voice_seconds"], 60)
            self.assertEqual(stats["gap_seconds"], 120)
            self.assertEqual((await restored.records(300))["longest_visit_seconds"], 30)
        finally:
            await restored.close()

    async def test_unreconciled_reports_can_disable_live_voice_extrapolation(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(150)
        self.assertEqual((await self.store.stats("all", 300))["voice_seconds"], 190)
        saved = await self.store.stats("all", 300, include_live=False)
        self.assertEqual(saved["voice_seconds"], 40)

    async def test_last_voice_follows_moves_leave_and_persists_past_detail_retention(self) -> None:
        self.assertIsNone(await self.store.last_voice(100))
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        self.assertEqual(await self.store.last_voice(120), {
            "channel_id": 101, "seen_at": 120, "current": True, "observed_since": 110,
        })
        await self.store.voice_transition(102, 130)
        self.assertEqual((await self.store.last_voice(140))["channel_id"], 102)
        await self.store.voice_transition(None, 150)
        self.assertEqual(await self.store.last_voice(160), {
            "channel_id": 102, "seen_at": 150, "current": False, "observed_since": None,
        })
        await self.store.maintenance(150 + 100 * 86400, 90)
        self.assertEqual((await self.store.last_voice(150 + 100 * 86400))["seen_at"], 150)
        await self.store.delete_data(22, 150 + 100 * 86400 + 1)
        self.assertIsNone(await self.store.last_voice(150 + 100 * 86400 + 2))

    async def test_last_voice_does_not_claim_current_during_disconnect(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(150)
        self.assertEqual((await self.store.last_voice(300, include_live=False))["seen_at"], 150)
        await self.store.disconnect(300)
        self.assertEqual(await self.store.last_voice(400), {
            "channel_id": 101, "seen_at": 150, "current": False, "observed_since": None,
        })

    async def test_schema_one_upgrade_recovers_last_retained_voice_observation(self) -> None:
        await self.store.connect(100)
        await self.store.voice_transition(101, 110)
        await self.store.checkpoint(150)
        await self.store.close()
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute("DROP TABLE last_voice")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        self.store = Store(self.db, self.backups, "UTC")
        await self.store.initialize(200, 11, 22)
        self.assertEqual((await self.store.last_voice(200))["seen_at"], 150)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)


    async def test_message_channel_counts_filter_by_period(self) -> None:
        now = epoch("2026-09-30T12:00:00")  # Wednesday; week starts 2026-09-28
        await self.store.add_message(501, 40, epoch("2026-09-27T12:00:00"))
        await self.store.add_message(502, 40, epoch("2026-09-29T08:00:00"))
        await self.store.add_message(503, 41, epoch("2026-09-29T09:00:00"))
        await self.store.add_message(504, 41, epoch("2026-09-30T10:00:00"))

        self.assertEqual(await self.store.message_channel_counts("week", now), {40: 1, 41: 2})
        self.assertEqual(await self.store.message_channel_counts("today", now), {41: 1})
        self.assertEqual(await self.store.message_channel_counts("all", now), {40: 2, 41: 2})
        self.assertEqual(
            await self.store.message_channel_counts("today", epoch("2026-10-02T12:00:00")), {}
        )
        with self.assertRaises(ValueError):
            await self.store.message_channel_counts("year", now)

    async def test_latest_messages_filter_order_and_limit(self) -> None:
        now = epoch("2026-09-30T12:00:00")
        same = epoch("2026-09-30T09:00:00")
        await self.store.add_message(601, 40, epoch("2026-09-27T12:00:00"))  # before week
        await self.store.add_message(602, 40, epoch("2026-09-29T08:00:00"))
        await self.store.add_message(603, 42, epoch("2026-09-29T09:00:00"))  # other channel
        await self.store.add_message(9, 41, same)
        await self.store.add_message(10, 41, same)
        await self.store.add_message(604, 41, epoch("2026-09-30T10:00:00"))

        rows = await self.store.latest_messages("week", now, {40, 41}, 10)
        self.assertEqual([row["message_id"] for row in rows], [604, 10, 9, 602])
        self.assertEqual(
            rows[0], {"message_id": 604, "channel_id": 41, "created_at": epoch("2026-09-30T10:00:00")}
        )
        limited = await self.store.latest_messages("week", now, [40, 41], 2)
        self.assertEqual([row["message_id"] for row in limited], [604, 10])
        all_rows = await self.store.latest_messages("all", now, [40], 10)
        self.assertEqual([row["message_id"] for row in all_rows], [602, 601])

    async def test_latest_messages_empty_inputs(self) -> None:
        now = epoch("2026-09-30T12:00:00")
        await self.store.add_message(701, 40, epoch("2026-09-30T10:00:00"))
        self.assertEqual(await self.store.latest_messages("week", now, [], 5), [])
        self.assertEqual(await self.store.latest_messages("week", now, [40], 0), [])
        self.assertEqual(await self.store.latest_messages("week", now, [99], 5), [])

if __name__ == "__main__":
    unittest.main()
