"""Tests for the one-time Leland Tracker (schema 9) to Flock CCTV import.

Every source database is synthetic and built from DDL embedded below (the old
schema-9 layout: base tables plus the migrated evil/reaction/full_seconds
columns). No production data is used.
"""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flock_cctv import legacy_import
from flock_cctv.legacy_import import LegacyImportError, import_legacy, main
from flock_cctv.storage import Store

GUILD = 700100200300400
LELAND = 800500600700800
OTHER_ADMIN = 900111222333444
COMPANION = 900555666777888
CHAN_A = 610000000000001
CHAN_B = 610000000000002
MSG_IDS = (510000000000001, 510000000000002, 510000000000003, 510000000000004)
TZ = "UTC"

# Distinctive values that must never appear in printed output.
SECRET_TEXT = {
    str(value)
    for value in (GUILD, LELAND, OTHER_ADMIN, COMPANION, CHAN_A, CHAN_B, *MSG_IDS)
}

SCHEMA_9_DDL = """
CREATE TABLE settings (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    guild_id TEXT NOT NULL,
    target_user_id TEXT NOT NULL,
    timezone TEXT NOT NULL,
    tracking_since REAL NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
    paused_by TEXT,
    connected INTEGER NOT NULL DEFAULT 0 CHECK (connected IN (0, 1)),
    last_checkpoint REAL,
    resume_boundary REAL NOT NULL,
    pruned_before REAL,
    retention_days INTEGER NOT NULL DEFAULT 90,
    evil_mode INTEGER NOT NULL DEFAULT 0 CHECK (evil_mode IN (0, 1)),
    reaction_mode INTEGER NOT NULL DEFAULT 0 CHECK (reaction_mode IN (0, 1)),
    reaction_countdown INTEGER
);
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    day TEXT NOT NULL
);
CREATE INDEX messages_created_at_idx ON messages(created_at);
CREATE TABLE daily_stats (
    day TEXT PRIMARY KEY,
    messages INTEGER NOT NULL DEFAULT 0,
    voice_seconds REAL NOT NULL DEFAULT 0,
    voice_visits INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))
);
CREATE TABLE voice_visits (
    visit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    complete_start INTEGER NOT NULL CHECK (complete_start IN (0, 1)),
    complete_end INTEGER NOT NULL DEFAULT 0 CHECK (complete_end IN (0, 1)),
    observed_seconds REAL NOT NULL DEFAULT 0
);
CREATE INDEX voice_visits_started_at_idx ON voice_visits(started_at);
CREATE TABLE voice_segments (
    segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    visit_id INTEGER NOT NULL REFERENCES voice_visits(visit_id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    checkpoint REAL NOT NULL
);
CREATE INDEX voice_segments_visit_idx ON voice_segments(visit_id);
CREATE UNIQUE INDEX one_open_voice_segment_idx ON voice_segments((1)) WHERE ended_at IS NULL;
CREATE TABLE voice_company_current (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    channel_id TEXT NOT NULL,
    member_ids TEXT NOT NULL,
    checkpoint REAL NOT NULL
);
CREATE TABLE voice_company_daily (
    day TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    seconds REAL NOT NULL,
    full_seconds REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, channel_id, member_id)
);
CREATE TABLE last_voice (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    channel_id TEXT NOT NULL,
    seen_at REAL NOT NULL
);
CREATE TABLE coverage_intervals (
    interval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    checkpoint REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT 'connected'
);
CREATE UNIQUE INDEX one_open_coverage_interval_idx ON coverage_intervals((1)) WHERE ended_at IS NULL;
CREATE TABLE coverage_gaps (
    gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    reason TEXT NOT NULL
);
CREATE INDEX coverage_gaps_bounds_idx ON coverage_gaps(started_at, ended_at);
CREATE TABLE records (
    record_type TEXT PRIMARY KEY,
    value REAL NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE admin_overrides (
    user_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);
"""


def epoch(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


SINCE = epoch("2025-02-01T00:00:00")
NOW = epoch("2025-02-10T12:00:00")
VISIT1_START = epoch("2025-02-03T10:00:00")
VISIT1_END = epoch("2025-02-03T11:00:00")
VISIT2_START = epoch("2025-02-04T09:00:00")
VISIT2_CHECKPOINT = epoch("2025-02-04T09:10:00")


def build_source(
    path: Path,
    *,
    wal: bool = False,
    version: int = 9,
    guild: int = GUILD,
    leland: int = LELAND,
    tz: str = TZ,
    open_state: bool = True,
    open_gap: bool = False,
    sequence_floor: bool = True,
) -> sqlite3.Connection:
    """Create a synthetic schema-9 database; return its still-open connection.

    With ``open_state`` the collector was connected with an open voice segment,
    live company roster and open coverage (a crash). ``open_gap`` adds an open
    coverage gap instead (a disconnected collector).
    """
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.executescript(SCHEMA_9_DDL)
    conn.execute(
        "INSERT INTO settings(singleton, guild_id, target_user_id, timezone, tracking_since, "
        "paused, paused_by, connected, last_checkpoint, resume_boundary, pruned_before, "
        "retention_days, evil_mode, reaction_mode, reaction_countdown) "
        "VALUES (1, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, 120, 1, 1, 7)",
        (
            str(guild),
            str(leland),
            tz,
            SINCE,
            int(open_state),
            VISIT2_CHECKPOINT,
            SINCE + 3600.0,
            SINCE - 86400.0,
        ),
    )
    for index, (message_id, moment) in enumerate(
        zip(
            MSG_IDS,
            (
                epoch("2025-02-03T09:00:00"),
                epoch("2025-02-03T09:30:00"),
                epoch("2025-02-03T12:00:00"),
                epoch("2025-02-04T09:05:00"),
            ),
        )
    ):
        day = datetime.fromtimestamp(moment, tz=timezone.utc).date().isoformat()
        conn.execute(
            "INSERT INTO messages(message_id, channel_id, created_at, day) VALUES (?, ?, ?, ?)",
            (str(message_id), str(CHAN_A if index % 2 == 0 else CHAN_B), moment, day),
        )
    conn.executemany(
        "INSERT INTO daily_stats(day, messages, voice_seconds, voice_visits, active) "
        "VALUES (?, ?, ?, ?, 1)",
        [("2025-02-03", 3, 3600.0, 1), ("2025-02-04", 1, 600.0, 1)],
    )
    conn.execute(
        "INSERT INTO voice_visits(visit_id, started_at, ended_at, complete_start, complete_end, "
        "observed_seconds) VALUES (1, ?, ?, 1, 1, 3600)",
        (VISIT1_START, VISIT1_END),
    )
    conn.execute(
        "INSERT INTO voice_segments(segment_id, visit_id, channel_id, started_at, ended_at, "
        "checkpoint) VALUES (1, 1, ?, ?, ?, ?)",
        (str(CHAN_A), VISIT1_START, VISIT1_END, VISIT1_END),
    )
    if open_state:
        conn.execute(
            "INSERT INTO voice_visits(visit_id, started_at, ended_at, complete_start, "
            "complete_end, observed_seconds) VALUES (2, ?, NULL, 1, 0, 600)",
            (VISIT2_START,),
        )
        conn.execute(
            "INSERT INTO voice_segments(segment_id, visit_id, channel_id, started_at, ended_at, "
            "checkpoint) VALUES (2, 2, ?, ?, NULL, ?)",
            (str(CHAN_B), VISIT2_START, VISIT2_CHECKPOINT),
        )
        conn.execute(
            "INSERT INTO voice_company_current(singleton, channel_id, member_ids, checkpoint) "
            "VALUES (1, ?, ?, ?)",
            (str(CHAN_B), f'["{COMPANION}"]', VISIT2_CHECKPOINT),
        )
    else:
        conn.execute(
            "INSERT INTO voice_visits(visit_id, started_at, ended_at, complete_start, "
            "complete_end, observed_seconds) VALUES (2, ?, ?, 1, 0, 600)",
            (VISIT2_START, VISIT2_CHECKPOINT),
        )
        conn.execute(
            "INSERT INTO voice_segments(segment_id, visit_id, channel_id, started_at, ended_at, "
            "checkpoint) VALUES (2, 2, ?, ?, ?, ?)",
            (str(CHAN_B), VISIT2_START, VISIT2_CHECKPOINT, VISIT2_CHECKPOINT),
        )
    conn.executemany(
        "INSERT INTO voice_company_daily(day, channel_id, member_id, seconds, full_seconds) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("2025-02-03", str(CHAN_A), str(COMPANION), 1800.0, 3600.0),
            ("2025-02-03", str(CHAN_A), "0", 1800.0, 1800.0),
            ("2025-02-04", str(CHAN_B), str(COMPANION), 600.0, 600.0),
        ],
    )
    conn.execute(
        "INSERT INTO last_voice(singleton, channel_id, seen_at) VALUES (1, ?, ?)",
        (str(CHAN_B), VISIT2_CHECKPOINT),
    )
    conn.execute(
        "INSERT INTO coverage_intervals(interval_id, started_at, ended_at, checkpoint, reason) "
        "VALUES (1, ?, ?, ?, 'connected')",
        (SINCE, epoch("2025-02-04T08:00:00"), epoch("2025-02-04T08:00:00")),
    )
    if open_state:
        conn.execute(
            "INSERT INTO coverage_intervals(interval_id, started_at, ended_at, checkpoint, "
            "reason) VALUES (2, ?, NULL, ?, 'connected')",
            (epoch("2025-02-04T08:30:00"), VISIT2_CHECKPOINT),
        )
    else:
        conn.execute(
            "INSERT INTO coverage_intervals(interval_id, started_at, ended_at, checkpoint, "
            "reason) VALUES (2, ?, ?, ?, 'connected')",
            (epoch("2025-02-04T08:30:00"), VISIT2_CHECKPOINT, VISIT2_CHECKPOINT),
        )
    conn.execute(
        "INSERT INTO coverage_gaps(gap_id, started_at, ended_at, reason) VALUES (1, ?, ?, "
        "'process_restart')",
        (epoch("2025-02-04T08:00:00"), epoch("2025-02-04T08:30:00")),
    )
    if open_gap:
        conn.execute(
            "INSERT INTO coverage_gaps(gap_id, started_at, ended_at, reason) VALUES (2, ?, "
            "NULL, 'disconnected')",
            (VISIT2_CHECKPOINT,),
        )
    conn.executemany(
        "INSERT INTO records(record_type, value, at) VALUES (?, ?, ?)",
        [("busiest_day", 3, "2025-02-03"), ("longest_visit", 3600.0, repr(VISIT1_START))],
    )
    conn.executemany(
        "INSERT INTO admin_overrides(user_id, enabled) VALUES (?, ?)",
        [(str(OTHER_ADMIN), 1), (str(COMPANION), 0)],
    )
    if sequence_floor:
        # Pruned newest rows leave the source counters ahead of the highest ID.
        conn.execute("UPDATE sqlite_sequence SET seq = 7 WHERE name = 'voice_visits'")
        conn.execute("UPDATE sqlite_sequence SET seq = 9 WHERE name = 'voice_segments'")
        conn.execute("UPDATE sqlite_sequence SET seq = 5 WHERE name = 'coverage_gaps'")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return conn


def table_rows(path: Path, query: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(query).fetchall()


class ImportTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "old.sqlite3"
        self.database = self.root / "new" / "tracker.sqlite3"
        self.backups = self.root / "new" / "backups"
        self.addCleanup(self.temp.cleanup)

    def make_source(self, **kwargs: object) -> None:
        build_source(self.source, **kwargs).close()  # type: ignore[arg-type]

    async def run_import(self, **overrides: object) -> dict[str, int]:
        args: dict[str, object] = {
            "source": self.source,
            "database": self.database,
            "backups": self.backups,
            "guild_id": GUILD,
            "leland_user_id": LELAND,
            "timezone": TZ,
        }
        args.update(overrides)
        return await import_legacy(**args)  # type: ignore[arg-type]

    @contextlib.asynccontextmanager
    async def new_store(self):
        store = Store(self.database, self.backups, TZ)
        await store.initialize(NOW, GUILD)
        try:
            yield store
        finally:
            await store.close()


class RoundTripTests(ImportTestCase):
    async def test_round_trip_matches_source_through_new_store(self) -> None:
        self.make_source()
        counts = await self.run_import()
        self.assertEqual(
            counts,
            {
                "messages": 4,
                "daily_stats": 2,
                "voice_visits": 2,
                "voice_segments": 2,
                "voice_company_current": 1,
                "voice_company_daily": 3,
                "last_voice": 1,
                "records": 2,
                "coverage_intervals": 2,
                "coverage_gaps": 1,
                "admin_overrides": 2,
                "tracked_users": 1,
                "tracking_intervals": 1,
                "recovered_open_segments": 1,
            },
        )
        async with self.new_store() as store:
            self.assertEqual(
                await store.tracked_users(),
                [
                    {
                        "user_id": LELAND,
                        "active": True,
                        "tracking_since": SINCE,
                        "added_by": None,
                        "updated_at": SINCE,
                    }
                ],
            )
            self.assertEqual(await store.active_user_ids(), frozenset({LELAND}))
            stats = await store.stats(LELAND, "all", NOW)
            self.assertEqual(stats["messages"], 4)
            self.assertEqual(stats["voice_seconds"], 4200.0)
            self.assertEqual(stats["voice_visits"], 2)
            self.assertEqual(stats["active_days"], 2)
            self.assertEqual(stats["tracking_since"], SINCE)
            self.assertTrue(stats["tracked"])
            self.assertFalse(stats["paused"])
            records = await store.records(LELAND, NOW)
            self.assertEqual(records["busiest_day"], "2025-02-03")
            self.assertEqual(records["busiest_day_messages"], 3)
            self.assertEqual(records["longest_visit_seconds"], 3600.0)
            self.assertEqual(records["longest_visit_at"], VISIT1_START)
            self.assertIsNone(records["current_visit_seconds"])
            last = await store.last_voice(LELAND, NOW)
            self.assertEqual(
                last,
                {
                    "channel_id": CHAN_B,
                    "seen_at": VISIT2_CHECKPOINT,
                    "current": False,
                    "observed_since": None,
                },
            )
            self.assertEqual(
                await store.company_totals(LELAND, "all", NOW),
                [
                    {"channel_id": CHAN_A, "member_id": 0, "seconds": 1800.0, "full_seconds": 1800.0},
                    {"channel_id": CHAN_A, "member_id": COMPANION, "seconds": 1800.0, "full_seconds": 3600.0},
                    {"channel_id": CHAN_B, "member_id": COMPANION, "seconds": 600.0, "full_seconds": 600.0},
                ],
            )
            trend = {row["day"]: row for row in await store.daily_trend(LELAND, "all", NOW)}
            self.assertEqual(trend["2025-02-03"]["messages"], 3)
            self.assertEqual(trend["2025-02-03"]["voice_seconds"], 3600.0)
            self.assertEqual(trend["2025-02-03"]["voice_visits"], 1)
            self.assertEqual(trend["2025-02-04"]["messages"], 1)
            self.assertEqual(trend["2025-02-04"]["voice_seconds"], 600.0)
            self.assertEqual(trend["2025-02-01"]["messages"], 0)
            self.assertTrue(trend["2025-02-02"]["watched"])
            self.assertEqual(await store.admin_overrides(), {OTHER_ADMIN: True, COMPANION: False})
            state = await store.state()
            self.assertTrue(state["evil_mode"])
            self.assertTrue(state["reaction_mode"])
            self.assertFalse(state["paused"])
            self.assertEqual(state["tracking_since"], SINCE)

    async def test_open_segment_is_recovered_as_incomplete_at_its_checkpoint(self) -> None:
        self.make_source()
        await self.run_import()
        visit = table_rows(
            self.database,
            "SELECT user_id, ended_at, complete_start, complete_end, observed_seconds "
            "FROM voice_visits WHERE visit_id = 2",
        )
        self.assertEqual(visit, [(str(LELAND), VISIT2_CHECKPOINT, 1, 0, 600.0)])
        segment = table_rows(
            self.database,
            "SELECT user_id, ended_at, checkpoint FROM voice_segments WHERE segment_id = 2",
        )
        self.assertEqual(segment, [(str(LELAND), VISIT2_CHECKPOINT, VISIT2_CHECKPOINT)])
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM voice_company_current"), [(0,)])
        self.assertEqual(
            table_rows(self.database, "SELECT COUNT(*) FROM coverage_intervals WHERE ended_at IS NULL"),
            [(0,)],
        )
        # A crashed collector leaves an open restart gap from its last checkpoint.
        self.assertEqual(
            table_rows(
                self.database,
                "SELECT started_at, reason FROM coverage_gaps WHERE ended_at IS NULL",
            ),
            [(VISIT2_CHECKPOINT, "process_restart")],
        )

    async def test_per_person_rows_use_leland_id_and_ids_are_preserved(self) -> None:
        self.make_source()
        await self.run_import()
        leland = str(LELAND)
        for table in (
            "messages",
            "daily_stats",
            "voice_visits",
            "voice_segments",
            "voice_company_daily",
            "last_voice",
            "records",
        ):
            self.assertEqual(
                table_rows(self.database, f"SELECT DISTINCT user_id FROM {table}"),
                [(leland,)],
                table,
            )
        self.assertEqual(
            table_rows(self.database, "SELECT visit_id FROM voice_visits ORDER BY visit_id"),
            [(1,), (2,)],
        )
        self.assertEqual(
            table_rows(self.database, "SELECT segment_id, visit_id FROM voice_segments ORDER BY 1"),
            [(1, 1), (2, 2)],
        )
        self.assertEqual(
            table_rows(self.database, "SELECT interval_id FROM coverage_intervals ORDER BY 1"),
            [(1,), (2,)],
        )
        self.assertEqual(
            table_rows(self.database, "SELECT gap_id, started_at, ended_at, reason FROM coverage_gaps ORDER BY 1")[0],
            (1, epoch("2025-02-04T08:00:00"), epoch("2025-02-04T08:30:00"), "process_restart"),
        )
        self.assertEqual(
            table_rows(self.database, "SELECT user_id, started_at, ended_at FROM tracking_intervals"),
            [(leland, SINCE, None)],
        )
        settings = table_rows(
            self.database,
            "SELECT guild_id, timezone, tracking_since, resume_boundary, pruned_before, "
            "retention_days, evil_mode, reaction_mode, reaction_countdown FROM settings",
        )
        self.assertEqual(
            settings,
            [(str(GUILD), TZ, SINCE, SINCE + 3600.0, SINCE - 86400.0, 120, 1, 1, 7)],
        )
        with closing(sqlite3.connect(self.database)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], Store.SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_new_rows_get_ids_beyond_every_copied_and_source_counter(self) -> None:
        self.make_source()
        await self.run_import()
        async with self.new_store() as store:
            await store.connect(NOW)
            await store.voice_transition(LELAND, CHAN_A, NOW + 1.0)
            await store.disconnect(NOW + 60.0)
        visit_ids = [r[0] for r in table_rows(self.database, "SELECT visit_id FROM voice_visits ORDER BY 1")]
        segment_ids = [r[0] for r in table_rows(self.database, "SELECT segment_id FROM voice_segments ORDER BY 1")]
        gap_ids = [r[0] for r in table_rows(self.database, "SELECT gap_id FROM coverage_gaps ORDER BY 1")]
        # The source counters stood at 7/9/5 with lower highest IDs; they are kept.
        self.assertEqual(visit_ids[:2], [1, 2])
        self.assertGreater(visit_ids[-1], 7)
        self.assertGreater(segment_ids[-1], 9)
        self.assertGreater(max(gap_ids), 5)
        self.assertEqual(len(visit_ids), 3)

    async def test_sequences_without_source_counter_floor_follow_copied_ids(self) -> None:
        self.make_source(sequence_floor=False)
        await self.run_import()
        async with self.new_store() as store:
            await store.connect(NOW)
            await store.voice_transition(LELAND, CHAN_A, NOW + 1.0)
            await store.disconnect(NOW + 60.0)
        self.assertEqual(
            table_rows(self.database, "SELECT (SELECT MAX(visit_id) FROM voice_visits), (SELECT MAX(segment_id) FROM voice_segments)"),
            [(3, 3)],
        )

    async def test_open_gap_and_closed_segment_state_are_copied_verbatim(self) -> None:
        self.make_source(open_state=False, open_gap=True)
        counts = await self.run_import()
        self.assertEqual(counts["coverage_gaps"], 2)
        self.assertEqual(counts["recovered_open_segments"], 0)
        self.assertEqual(
            table_rows(
                self.database,
                "SELECT gap_id, started_at, reason FROM coverage_gaps WHERE ended_at IS NULL",
            ),
            [(2, VISIT2_CHECKPOINT, "disconnected")],
        )
        # A disconnected source starts no restart gap of its own.
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM coverage_gaps"), [(2,)])

    async def test_paused_state_is_copied(self) -> None:
        self.make_source(open_state=False)
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute("UPDATE settings SET paused = 1, paused_by = ?", (str(OTHER_ADMIN),))
            conn.commit()
        await self.run_import()
        self.assertEqual(
            table_rows(self.database, "SELECT paused, paused_by FROM settings"),
            [(1, str(OTHER_ADMIN))],
        )

    async def test_wal_source_with_uncheckpointed_data_is_read(self) -> None:
        live = build_source(self.source, wal=True)
        try:
            wal_file = self.source.with_name(self.source.name + "-wal")
            self.assertGreater(wal_file.stat().st_size, 0)
            main_before = self.source.read_bytes()
            counts = await self.run_import()
            self.assertEqual(counts["messages"], 4)
            self.assertEqual(counts["voice_visits"], 2)
            # The source connection stays usable and its main file is untouched.
            self.assertEqual(self.source.read_bytes(), main_before)
            self.assertEqual(live.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4)
        finally:
            live.close()
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM messages"), [(4,)])

    async def test_source_is_never_modified(self) -> None:
        self.make_source()
        before_bytes = self.source.read_bytes()
        before_mtime = self.source.stat().st_mtime_ns
        before_dir = sorted(p.name for p in self.root.iterdir())
        await self.run_import()
        self.assertEqual(self.source.read_bytes(), before_bytes)
        self.assertEqual(self.source.stat().st_mtime_ns, before_mtime)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), sorted([*before_dir, "new"]))
        self.assertEqual(table_rows(self.source, "SELECT COUNT(*) FROM voice_segments WHERE ended_at IS NULL"), [(1,)])

    async def test_existing_empty_destination_file_is_accepted(self) -> None:
        self.make_source()
        self.database.parent.mkdir(parents=True)
        sqlite3.connect(self.database).close()
        await self.run_import()
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM messages"), [(4,)])


class RefusalTests(ImportTestCase):
    async def assert_refused(self, message_part: str, **overrides: object) -> None:
        with self.assertRaises(LegacyImportError) as caught:
            await self.run_import(**overrides)
        self.assertIn(message_part, str(caught.exception))
        for secret in SECRET_TEXT:
            self.assertNotIn(secret, str(caught.exception))
        self.assertFalse(self.database.exists(), "no destination may be left behind")
        for suffix in ("-wal", "-shm"):
            self.assertFalse(self.database.with_name(self.database.name + suffix).exists())

    async def test_wrong_user_version(self) -> None:
        for version in (0, 8, 10):
            self.source.unlink(missing_ok=True)
            self.make_source(version=version)
            await self.assert_refused("schema version", )

    async def test_wrong_guild(self) -> None:
        self.make_source()
        await self.assert_refused("different guild", guild_id=GUILD + 1)

    async def test_wrong_leland_id(self) -> None:
        self.make_source()
        await self.assert_refused("different user", leland_user_id=LELAND + 1)

    async def test_wrong_timezone(self) -> None:
        self.make_source()
        await self.assert_refused("timezone differs", timezone="America/Costa_Rica")

    async def test_invalid_timezone_and_ids(self) -> None:
        self.make_source()
        await self.assert_refused("IANA", timezone="Not/AZone")
        await self.assert_refused("positive", guild_id=0)

    async def test_missing_and_non_sqlite_source(self) -> None:
        await self.assert_refused("source database")
        self.source.write_bytes(b"this is not a sqlite database" * 100)
        await self.assert_refused("source")

    async def test_source_without_leland_columns_is_refused(self) -> None:
        # A Flock/new-style database (or anything else) is not a schema-9 source.
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute("CREATE TABLE settings (singleton INTEGER)")
            conn.execute("PRAGMA user_version = 9")
            conn.commit()
        await self.assert_refused("missing tables")

    async def test_database_cannot_be_the_source(self) -> None:
        self.make_source()
        await self.assert_refused("different file", database=self.source)
        self.assertEqual(table_rows(self.source, "SELECT COUNT(*) FROM messages"), [(4,)])

    async def test_initialized_destination_is_refused_and_left_untouched(self) -> None:
        self.make_source()
        existing = Store(self.database, self.backups, TZ)
        await existing.initialize(NOW, GUILD)
        await existing.track_user(LELAND + 5, OTHER_ADMIN, NOW)
        await existing.close()
        before = table_rows(self.database, "SELECT * FROM settings")
        before_tracked = table_rows(self.database, "SELECT * FROM tracked_users")
        before_bytes = self.database.read_bytes()
        with self.assertRaises(LegacyImportError) as caught:
            await self.run_import()
        self.assertIn("already has settings", str(caught.exception))
        self.assertTrue(self.database.exists())
        self.assertEqual(table_rows(self.database, "SELECT * FROM settings"), before)
        self.assertEqual(table_rows(self.database, "SELECT * FROM tracked_users"), before_tracked)
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM messages"), [(0,)])
        self.assertEqual(self.database.read_bytes(), before_bytes)

    async def test_dry_run_also_refuses_initialized_destination(self) -> None:
        self.make_source()
        existing = Store(self.database, self.backups, TZ)
        await existing.initialize(NOW, GUILD)
        await existing.close()
        with self.assertRaises(LegacyImportError):
            await self.run_import(dry_run=True)

    async def test_stale_sidecars_without_database_are_refused(self) -> None:
        self.make_source()
        self.database.parent.mkdir(parents=True)
        stale = self.database.with_name(self.database.name + "-wal")
        stale.write_bytes(b"stale")
        with self.assertRaises(LegacyImportError):
            await self.run_import()
        self.assertEqual(stale.read_bytes(), b"stale")

    async def test_backups_inside_database_location_is_refused(self) -> None:
        self.make_source()
        with self.assertRaises(LegacyImportError):
            await self.run_import(backups=self.database.parent)  # contains the database


class FailureCleanupTests(ImportTestCase):
    async def test_failure_removes_a_brand_new_destination(self) -> None:
        self.make_source()
        with patch.object(
            legacy_import, "_copy_and_verify", side_effect=LegacyImportError("boom")
        ):
            with self.assertRaises(LegacyImportError):
                await self.run_import()
        self.assertFalse(self.database.exists())
        for suffix in ("-wal", "-shm"):
            self.assertFalse(self.database.with_name(self.database.name + suffix).exists())
        # A retry is clean and succeeds.
        await self.run_import()
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM messages"), [(4,)])

    async def test_verification_failure_after_recovery_removes_destination(self) -> None:
        self.make_source()
        with patch.object(
            legacy_import, "_verify_after_recovery", side_effect=LegacyImportError("bad")
        ):
            with self.assertRaises(LegacyImportError):
                await self.run_import()
        self.assertFalse(self.database.exists())

    async def test_failure_never_deletes_a_preexisting_destination(self) -> None:
        self.make_source()
        self.database.parent.mkdir(parents=True)
        sqlite3.connect(self.database).close()
        with patch.object(
            legacy_import, "_copy_and_verify", side_effect=LegacyImportError("boom")
        ):
            with self.assertRaises(LegacyImportError):
                await self.run_import()
        self.assertTrue(self.database.exists())

    async def test_copy_error_rolls_back_in_one_transaction(self) -> None:
        self.make_source()
        # A duplicate open coverage interval cannot exist in the source, so break
        # the copy itself: the last copied table fails after earlier ones succeeded.
        real_insert = legacy_import._advance_sequences

        def failing(snapshot: sqlite3.Connection, destination: sqlite3.Connection) -> None:
            real_insert(snapshot, destination)
            raise sqlite3.IntegrityError("simulated")

        with patch.object(legacy_import, "_advance_sequences", failing):
            with self.assertRaises(LegacyImportError) as caught:
                await self.run_import()
        self.assertIn("rolled back", str(caught.exception))
        self.assertFalse(self.database.exists())


class DryRunTests(ImportTestCase):
    async def test_dry_run_writes_nothing_and_returns_counts(self) -> None:
        self.make_source()
        before = sorted(p.name for p in self.root.iterdir())
        counts = await self.run_import(dry_run=True)
        self.assertEqual(counts["messages"], 4)
        self.assertEqual(counts["voice_segments"], 2)
        self.assertEqual(counts["recovered_open_segments"], 1)
        self.assertEqual(counts["tracked_users"], 1)
        self.assertFalse(self.database.exists())
        self.assertFalse(self.database.parent.exists())
        self.assertFalse(self.backups.exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before)

    async def test_dry_run_counts_match_real_import(self) -> None:
        self.make_source()
        dry = await self.run_import(dry_run=True)
        real = await self.run_import()
        self.assertEqual(dry, real)

    async def test_dry_run_validates_source(self) -> None:
        self.make_source()
        with self.assertRaises(LegacyImportError):
            await self.run_import(dry_run=True, guild_id=GUILD + 1)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "old.sqlite3"
        self.database = self.root / "out" / "tracker.sqlite3"
        self.backups = self.root / "out" / "backups"
        build_source(self.source).close()
        self.clean_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"DATABASE_PATH", "BACKUP_DIR", "GUILD_ID", "LELAND_USER_ID", "TIMEZONE"}
        }

    def run_main(self, argv: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, env or {}, clear=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:  # argparse usage errors
                code = int(exc.code) if exc.code is not None else 0
        return code, out.getvalue(), err.getvalue()

    def full_args(self) -> list[str]:
        return [
            "--source", str(self.source),
            "--database", str(self.database),
            "--backups", str(self.backups),
            "--guild-id", str(GUILD),
            "--leland-user-id", str(LELAND),
            "--timezone", TZ,
        ]

    def assert_no_secrets(self, text: str) -> None:
        for secret in SECRET_TEXT:
            self.assertNotIn(secret, text)

    def test_successful_import_prints_counts_only(self) -> None:
        with patch.dict(os.environ, self.clean_env, clear=True):
            code, out, err = self.run_main(self.full_args())
        self.assertEqual(code, 0, err)
        self.assertIn("Import complete.", out)
        self.assertIn("messages: 4", out)
        self.assertIn("voice_visits: 2", out)
        self.assertIn("closed by recovery as incomplete: 1", out)
        self.assert_no_secrets(out)
        self.assert_no_secrets(err)
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM messages"), [(4,)])

    def test_dry_run_flag(self) -> None:
        with patch.dict(os.environ, self.clean_env, clear=True):
            code, out, err = self.run_main([*self.full_args(), "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertIn("Dry run", out)
        self.assertIn("messages: 4", out)
        self.assert_no_secrets(out)
        self.assertFalse(self.database.exists())

    def test_environment_fallbacks(self) -> None:
        env = {
            **self.clean_env,
            "DATABASE_PATH": str(self.database),
            "BACKUP_DIR": str(self.backups),
            "GUILD_ID": str(GUILD),
            "LELAND_USER_ID": str(LELAND),
            "TIMEZONE": TZ,
        }
        with patch.dict(os.environ, env, clear=True):
            code, out, err = self.run_main(["--source", str(self.source)])
        self.assertEqual(code, 0, err)
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM messages"), [(4,)])

    def test_flags_override_environment(self) -> None:
        env = {**self.clean_env, "GUILD_ID": str(GUILD + 9), "LELAND_USER_ID": str(LELAND + 9)}
        with patch.dict(os.environ, env, clear=True):
            code, out, err = self.run_main(self.full_args())
        self.assertEqual(code, 0, err)

    def test_missing_required_values_exit_nonzero(self) -> None:
        with patch.dict(os.environ, self.clean_env, clear=True):
            code, _, err = self.run_main(["--source", str(self.source), "--database", str(self.database)])
            self.assertEqual(code, 2)
            self.assertIn("--guild-id", err)
            code, _, err = self.run_main([])
            self.assertEqual(code, 2)
            args = self.full_args()
            args[args.index("--guild-id") + 1] = "abc"
            code, _, err = self.run_main(args)
            self.assertEqual(code, 2)
        self.assertFalse(self.database.exists())

    def test_validation_failures_exit_one_without_ids(self) -> None:
        cases = {
            "--guild-id": str(GUILD + 1),
            "--leland-user-id": str(LELAND + 1),
            "--timezone": "America/Costa_Rica",
        }
        with patch.dict(os.environ, self.clean_env, clear=True):
            for flag, value in cases.items():
                args = self.full_args()
                args[args.index(flag) + 1] = value
                code, out, err = self.run_main(args)
                self.assertEqual(code, 1, flag)
                self.assertIn("Import failed:", err)
                self.assertEqual(out, "")
                self.assert_no_secrets(err)
                self.assertFalse(self.database.exists())

    def test_missing_source_and_existing_destination_exit_one(self) -> None:
        with patch.dict(os.environ, self.clean_env, clear=True):
            args = self.full_args()
            args[1] = str(self.root / "missing.sqlite3")
            code, _, err = self.run_main(args)
            self.assertEqual(code, 1)
            self.assertIn("source database", err)
            self.assertEqual(self.run_main(self.full_args())[0], 0)
            code, out, err = self.run_main(self.full_args())  # second run refuses
        self.assertEqual(code, 1)
        self.assertIn("already has settings", err)
        self.assert_no_secrets(err)
        self.assertEqual(table_rows(self.database, "SELECT COUNT(*) FROM messages"), [(4,)])


if __name__ == "__main__":
    unittest.main()
