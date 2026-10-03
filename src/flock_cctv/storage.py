"""Serialized asynchronous SQLite storage for the Leland Tracker.

The SQLite connection lives on one dedicated worker thread. All schema changes,
collector updates, report snapshots, backups, and deletion therefore run in a
single ordered queue without blocking the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Collection, TypeVar

from .stats import (
    get_timezone,
    interval_overlap,
    local_date,
    local_day,
    local_midnight,
    period_bounds,
    previous_period_bounds,
    split_interval_by_day,
    split_interval_by_hour,
)

_T = TypeVar("_T")
logger = logging.getLogger(__name__)
_BACKUP_RE = re.compile(r"^flock-cctv-(\d{4}-\d{2}-\d{2})\.sqlite3$")
_BACKUP_TEMP_RE = re.compile(
    r"^\.flock-cctv-(\d{4}-\d{2}-\d{2})\.[A-Za-z0-9_-]+\.sqlite3\.tmp$"
)
_NAMED_BACKUPS = {
    "flock-cctv-manual.sqlite3",
    "flock-cctv-before-restore.sqlite3",
    "flock-cctv-before-update.sqlite3",
}


def _is_managed_backup(name: str) -> bool:
    # A crash can leave a SQLite sidecar alongside a temporary or named backup.
    for suffix in ("-wal", "-shm", "-journal"):
        if name.endswith(suffix):
            name = name[:-len(suffix)]
            break
    return bool(name in _NAMED_BACKUPS or _BACKUP_RE.fullmatch(name) or _BACKUP_TEMP_RE.fullmatch(name))


class StoreError(RuntimeError):
    """Base error for invalid or unavailable store operations."""


class Store:
    """Async storage service; construct once and close it during shutdown."""

    SCHEMA_VERSION = 9
    DEFAULT_RETENTION_DAYS = 90
    # A tracker reconnect or restart closes the open visit at its last
    # checkpoint. If Leland is found in the same channel this soon afterwards,
    # the visit continues instead of splitting into two incomplete visits. The
    # uncovered interval is still a coverage gap and is never counted.
    VISIT_BRIDGE_SECONDS = 120.0

    def __init__(self, path: Path, backup_dir: Path, timezone: str) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir)
        self.timezone = timezone
        get_timezone(timezone)  # Fail early for invalid IANA names.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="leland-sqlite")
        self._lock = asyncio.Lock()
        self._connection: sqlite3.Connection | None = None
        self._closed = False

    async def _run(self, operation: Callable[[], _T]) -> _T:
        async with self._lock:
            if self._closed:
                raise StoreError("store is closed")
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, operation)

    def _conn(self) -> sqlite3.Connection:
        # This method is called only from the store's single worker thread.
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            self._connection = conn
        return self._connection

    @staticmethod
    def _timestamp(value: float) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("timestamp must be finite")
        return result

    @staticmethod
    def _transaction(conn: sqlite3.Connection, action: Callable[[], _T]) -> _T:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = action()
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _settings(conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM settings WHERE singleton = 1").fetchone()
        if row is None:
            raise StoreError("store is not initialized")
        return row

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > self.SCHEMA_VERSION:
            raise StoreError(
                f"database schema version {version} is newer than supported "
                f"version {self.SCHEMA_VERSION}"
            )

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
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
                retention_days INTEGER NOT NULL DEFAULT 90
            );

            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                day TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_created_at_idx ON messages(created_at);

            CREATE TABLE IF NOT EXISTS daily_stats (
                day TEXT PRIMARY KEY,
                messages INTEGER NOT NULL DEFAULT 0,
                voice_seconds REAL NOT NULL DEFAULT 0,
                voice_visits INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS voice_visits (
                visit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                ended_at REAL,
                complete_start INTEGER NOT NULL CHECK (complete_start IN (0, 1)),
                complete_end INTEGER NOT NULL DEFAULT 0 CHECK (complete_end IN (0, 1)),
                observed_seconds REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS voice_visits_started_at_idx ON voice_visits(started_at);

            CREATE TABLE IF NOT EXISTS voice_segments (
                segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                visit_id INTEGER NOT NULL REFERENCES voice_visits(visit_id) ON DELETE CASCADE,
                channel_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                checkpoint REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS voice_segments_visit_idx ON voice_segments(visit_id);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_voice_segment_idx
                ON voice_segments((1)) WHERE ended_at IS NULL;

            CREATE TABLE IF NOT EXISTS voice_company_current (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                channel_id TEXT NOT NULL,
                member_ids TEXT NOT NULL,
                checkpoint REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_company_daily (
                day TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                seconds REAL NOT NULL,
                full_seconds REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (day, channel_id, member_id)
            );

            CREATE TABLE IF NOT EXISTS last_voice (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                channel_id TEXT NOT NULL,
                seen_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS coverage_intervals (
                interval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                ended_at REAL,
                checkpoint REAL NOT NULL,
                reason TEXT NOT NULL DEFAULT 'connected'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_coverage_interval_idx
                ON coverage_intervals((1)) WHERE ended_at IS NULL;

            CREATE TABLE IF NOT EXISTS coverage_gaps (
                gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at REAL NOT NULL,
                ended_at REAL,
                reason TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS coverage_gaps_bounds_idx
                ON coverage_gaps(started_at, ended_at);

            CREATE TABLE IF NOT EXISTS records (
                record_type TEXT PRIMARY KEY,
                value REAL NOT NULL,
                at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_overrides (
                user_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
            );
            """
        )
        if version < self.SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
                if "evil_mode" not in columns:
                    conn.execute(
                        "ALTER TABLE settings ADD COLUMN evil_mode INTEGER NOT NULL DEFAULT 0 "
                        "CHECK (evil_mode IN (0, 1))"
                    )
                if "reaction_mode" not in columns:
                    conn.execute(
                        "ALTER TABLE settings ADD COLUMN reaction_mode INTEGER NOT NULL DEFAULT 0 "
                        "CHECK (reaction_mode IN (0, 1))"
                    )
                if "reaction_countdown" not in columns:
                    conn.execute(
                        "ALTER TABLE settings ADD COLUMN reaction_countdown INTEGER"
                    )
                company_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(voice_company_daily)")
                }
                if "full_seconds" not in company_columns:
                    conn.execute(
                        "ALTER TABLE voice_company_daily "
                        "ADD COLUMN full_seconds REAL NOT NULL DEFAULT 0"
                    )
                if version < 9:
                    # Whole shared time cannot be rebuilt for time recorded before
                    # schema 7, so it counts as its split share. A day that spans
                    # the upgrade holds both kinds; whole time is never below the
                    # split, so raising every row to at least its split keeps that
                    # day's earlier time instead of dropping it.
                    conn.execute(
                        "UPDATE voice_company_daily SET full_seconds = seconds "
                        "WHERE full_seconds < seconds"
                    )
                # The previous schema already has voice_segments. Backfill the
                # latest retained observation before old detail is pruned.
                conn.execute(
                    """INSERT OR IGNORE INTO last_voice(singleton, channel_id, seen_at)
                       SELECT 1, channel_id, checkpoint FROM voice_segments
                       ORDER BY checkpoint DESC, segment_id DESC LIMIT 1"""
                )
                conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _daily_delta(
        conn: sqlite3.Connection,
        day: str,
        *,
        messages: int = 0,
        voice_seconds: float = 0.0,
        voice_visits: int = 0,
    ) -> None:
        conn.execute(
            """INSERT INTO daily_stats(day, messages, voice_seconds, voice_visits, active)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(day) DO UPDATE SET
                   messages = daily_stats.messages + excluded.messages,
                   voice_seconds = daily_stats.voice_seconds + excluded.voice_seconds,
                   voice_visits = daily_stats.voice_visits + excluded.voice_visits,
                   active = CASE WHEN daily_stats.active = 1 OR excluded.active = 1
                                 THEN 1 ELSE 0 END""",
            (day, messages, voice_seconds, voice_visits,
             int(messages > 0 or voice_seconds > 0.0)),
        )

    def _add_voice_time(
        self, conn: sqlite3.Connection, visit_id: int, start: float, end: float
    ) -> float:
        if end <= start:
            return 0.0
        delta = end - start
        for day, seconds in split_interval_by_day(start, end, self.timezone):
            if seconds > 0:
                self._daily_delta(conn, day, voice_seconds=seconds)
        self._add_company_time(conn, start, end)
        conn.execute(
            "UPDATE voice_visits SET observed_seconds = observed_seconds + ? WHERE visit_id = ?",
            (delta, visit_id),
        )
        return delta

    def _add_company_time(self, conn: sqlite3.Connection, start: float, end: float) -> None:
        row = conn.execute("SELECT * FROM voice_company_current WHERE singleton = 1").fetchone()
        if row is None or end <= start:
            return
        members = json.loads(row["member_ids"])
        recipients = members if members else ["0"]  # 0 is the alone slice.
        for day, seconds in split_interval_by_day(start, end, self.timezone):
            for member_id in recipients:
                conn.execute(
                    """INSERT INTO voice_company_daily(day, channel_id, member_id, seconds, full_seconds)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(day, channel_id, member_id) DO UPDATE SET
                           seconds = seconds + excluded.seconds,
                           full_seconds = full_seconds + excluded.full_seconds""",
                    (day, row["channel_id"], member_id, seconds / len(recipients), seconds),
                )
        conn.execute(
            "UPDATE voice_company_current SET checkpoint = ? WHERE singleton = 1", (end,)
        )

    @staticmethod
    def _open_segment(conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM voice_segments WHERE ended_at IS NULL ORDER BY segment_id DESC LIMIT 1"
        ).fetchone()

    @staticmethod
    def _open_coverage(conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM coverage_intervals WHERE ended_at IS NULL "
            "ORDER BY interval_id DESC LIMIT 1"
        ).fetchone()

    @staticmethod
    def _record_last_voice(conn: sqlite3.Connection, channel_id: str, seen_at: float) -> None:
        conn.execute(
            """INSERT INTO last_voice(singleton, channel_id, seen_at) VALUES (1, ?, ?)
               ON CONFLICT(singleton) DO UPDATE SET
                   channel_id = excluded.channel_id, seen_at = excluded.seen_at
               WHERE excluded.seen_at >= last_voice.seen_at""",
            (channel_id, seen_at),
        )

    def _close_voice_segment(
        self, conn: sqlite3.Connection, segment: sqlite3.Row, at: float
    ) -> float:
        close_at = max(float(segment["checkpoint"]), at)
        delta = self._add_voice_time(
            conn, int(segment["visit_id"]), float(segment["checkpoint"]), close_at
        )
        conn.execute(
            "UPDATE voice_segments SET ended_at = ?, checkpoint = ? WHERE segment_id = ?",
            (close_at, close_at, int(segment["segment_id"])),
        )
        conn.execute("DELETE FROM voice_company_current WHERE singleton = 1")
        self._record_last_voice(conn, str(segment["channel_id"]), close_at)
        return close_at

    def _finish_visit(
        self,
        conn: sqlite3.Connection,
        visit_id: int,
        at: float,
        *,
        complete_end: bool,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM voice_visits WHERE visit_id = ?", (visit_id,)
        ).fetchone()
        if row is None or row["ended_at"] is not None:
            return
        ended_at = max(float(row["started_at"]), at)
        conn.execute(
            "UPDATE voice_visits SET ended_at = ?, complete_end = ? WHERE visit_id = ?",
            (ended_at, int(complete_end), visit_id),
        )
        if bool(row["complete_start"]) and complete_end:
            self._update_longest_visit(
                conn,
                float(row["observed_seconds"]),
                float(row["started_at"]),
            )

    @staticmethod
    def _visit_channel(conn: sqlite3.Connection, visit_id: int, *, last: bool) -> str | None:
        order = "DESC" if last else "ASC"
        row = conn.execute(
            f"SELECT channel_id FROM voice_segments WHERE visit_id = ? "
            f"ORDER BY started_at {order}, segment_id {order} LIMIT 1",
            (visit_id,),
        ).fetchone()
        return None if row is None else str(row["channel_id"])

    def _bridges(
        self, conn: sqlite3.Connection, previous: sqlite3.Row, channel_id: str, at: float
    ) -> bool:
        """True when a reconciled visit continues ``previous`` across a tracker outage.

        Only a visit cut short by a disconnect or restart qualifies: a pause,
        a departure, or an outage longer than the bridge window keeps the split.
        """
        if previous["ended_at"] is None or bool(previous["complete_end"]):
            return False
        ended_at = float(previous["ended_at"])
        if not 0 <= at - ended_at <= self.VISIT_BRIDGE_SECONDS:
            return False
        if self._visit_channel(conn, int(previous["visit_id"]), last=True) != channel_id:
            return False
        return conn.execute(
            """SELECT 1 FROM coverage_gaps
               WHERE reason IN ('disconnect', 'process_restart')
                 AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)
               LIMIT 1""",
            (ended_at, at),
        ).fetchone() is not None

    def _recompute_longest_visit(self, conn: sqlite3.Connection) -> None:
        """Apply the bridge rule to retained visits split before it existed."""
        rows = conn.execute(
            "SELECT * FROM voice_visits WHERE ended_at IS NOT NULL "
            "ORDER BY started_at, visit_id"
        ).fetchall()
        chain_start: sqlite3.Row | None = None
        previous: sqlite3.Row | None = None
        seconds = 0.0
        for row in rows:
            channel = self._visit_channel(conn, int(row["visit_id"]), last=False)
            if (
                previous is not None
                and chain_start is not None
                and not bool(row["complete_start"])
                and channel is not None
                and self._bridges(conn, previous, channel, float(row["started_at"]))
            ):
                seconds += float(row["observed_seconds"])
            else:
                chain_start = row
                seconds = float(row["observed_seconds"])
            previous = row
            if bool(chain_start["complete_start"]) and bool(row["complete_end"]):
                self._update_longest_visit(conn, seconds, float(chain_start["started_at"]))

    @staticmethod
    def _update_longest_visit(
        conn: sqlite3.Connection, duration: float, started_at: float
    ) -> None:
        current = conn.execute(
            "SELECT value FROM records WHERE record_type = 'longest_visit'"
        ).fetchone()
        if current is None or duration > float(current["value"]):
            conn.execute(
                "INSERT INTO records(record_type, value, at) VALUES ('longest_visit', ?, ?) "
                "ON CONFLICT(record_type) DO UPDATE SET value = excluded.value, at = excluded.at",
                (duration, repr(started_at)),
            )

    def _update_busiest_day(self, conn: sqlite3.Connection, day: str) -> None:
        count_row = conn.execute(
            "SELECT messages FROM daily_stats WHERE day = ?", (day,)
        ).fetchone()
        count = int(count_row["messages"])
        existing = conn.execute(
            "SELECT value, at FROM records WHERE record_type = 'busiest_day'"
        ).fetchone()
        if existing is None or count > int(existing["value"]) or (
            count == int(existing["value"]) and day < str(existing["at"])
        ):
            conn.execute(
                "INSERT INTO records(record_type, value, at) VALUES ('busiest_day', ?, ?) "
                "ON CONFLICT(record_type) DO UPDATE SET value = excluded.value, at = excluded.at",
                (count, day),
            )

    async def initialize(self, now: float, guild_id: int, target_user_id: int) -> None:
        """Create or validate the database and recover any previous open session."""
        now = self._timestamp(now)
        guild_id_text = str(int(guild_id))
        target_text = str(int(target_user_id))

        def operation() -> None:
            conn = self._conn()
            self._create_schema(conn)

            def action() -> None:
                row = conn.execute("SELECT * FROM settings WHERE singleton = 1").fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO settings(
                               singleton, guild_id, target_user_id, timezone,
                               tracking_since, resume_boundary
                           ) VALUES (1, ?, ?, ?, ?, ?)""",
                        (guild_id_text, target_text, self.timezone, now, now),
                    )
                    return
                if row["guild_id"] != guild_id_text or row["target_user_id"] != target_text:
                    raise StoreError("database belongs to a different guild or target user")
                if row["timezone"] != self.timezone:
                    raise StoreError("database timezone differs from configured timezone")

                was_connected = bool(row["connected"])
                intentionally_paused = bool(row["paused"])
                open_segments = conn.execute(
                    "SELECT * FROM voice_segments WHERE ended_at IS NULL"
                ).fetchall()
                recovery_point: float | None = None
                for segment in open_segments:
                    checkpoint = float(segment["checkpoint"])
                    recovery_point = checkpoint if recovery_point is None else min(
                        recovery_point, checkpoint
                    )
                    conn.execute(
                        "UPDATE voice_segments SET ended_at = ?, checkpoint = ? "
                        "WHERE segment_id = ?",
                        (checkpoint, checkpoint, int(segment["segment_id"])),
                    )
                    self._finish_visit(
                        conn,
                        int(segment["visit_id"]),
                        checkpoint,
                        complete_end=False,
                    )
                conn.execute("DELETE FROM voice_company_current")

                coverage = self._open_coverage(conn)
                if coverage is not None:
                    checkpoint = float(coverage["checkpoint"])
                    recovery_point = checkpoint if recovery_point is None else min(
                        recovery_point, checkpoint
                    )
                    conn.execute(
                        "UPDATE coverage_intervals SET ended_at = ? WHERE interval_id = ?",
                        (checkpoint, int(coverage["interval_id"])),
                    )

                if was_connected and not intentionally_paused:
                    gap_start = recovery_point
                    if gap_start is None and row["last_checkpoint"] is not None:
                        gap_start = float(row["last_checkpoint"])
                    if gap_start is not None:
                        self._start_gap(conn, gap_start, "process_restart")
                conn.execute(
                    "UPDATE settings SET connected = 0 WHERE singleton = 1"
                )
                self._recompute_longest_visit(conn)

            self._transaction(conn, action)

        await self._run(operation)

    @staticmethod
    def _insert_gap(
        conn: sqlite3.Connection, start: float, end: float, reason: str
    ) -> None:
        if end > start:
            conn.execute(
                "INSERT INTO coverage_gaps(started_at, ended_at, reason) VALUES (?, ?, ?)",
                (start, end, reason),
            )

    @staticmethod
    def _start_gap(conn: sqlite3.Connection, start: float, reason: str) -> None:
        if conn.execute(
            "SELECT 1 FROM coverage_gaps WHERE ended_at IS NULL LIMIT 1"
        ).fetchone() is None:
            conn.execute(
                "INSERT INTO coverage_gaps(started_at, ended_at, reason) VALUES (?, NULL, ?)",
                (start, reason),
            )

    @staticmethod
    def _close_open_gap(conn: sqlite3.Connection, at: float) -> None:
        row = conn.execute(
            "SELECT gap_id, started_at FROM coverage_gaps WHERE ended_at IS NULL "
            "ORDER BY gap_id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            end = max(float(row["started_at"]), at)
            conn.execute(
                "UPDATE coverage_gaps SET ended_at = ? WHERE gap_id = ?",
                (end, int(row["gap_id"])),
            )

    async def close(self) -> None:
        """Close SQLite and stop the dedicated worker thread."""
        async with self._lock:
            if self._closed:
                return
            loop = asyncio.get_running_loop()
            connection = self._connection
            self._closed = True
            if connection is not None:
                await loop.run_in_executor(self._executor, connection.close)
                self._connection = None
            self._executor.shutdown(wait=True)

    async def state(self) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            row = self._settings(self._conn())
            return {
                "paused": bool(row["paused"]),
                "evil_mode": bool(row["evil_mode"]),
                "reaction_mode": bool(row["reaction_mode"]),
                "paused_by": row["paused_by"],
                "tracking_since": float(row["tracking_since"]),
                "last_checkpoint": (
                    None if row["last_checkpoint"] is None else float(row["last_checkpoint"])
                ),
            }

        return await self._run(operation)

    async def set_evil_mode(self, enabled: bool) -> None:
        """Persist the admin-controlled message echo switch."""

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                self._settings(conn)
                conn.execute(
                    "UPDATE settings SET evil_mode = ? WHERE singleton = 1",
                    (int(enabled),),
                )

            self._transaction(conn, action)

        await self._run(operation)

    async def set_reaction_mode(self, enabled: bool) -> None:
        """Persist the reaction switch and start a fresh interval when enabled."""

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                self._settings(conn)
                conn.execute(
                    "UPDATE settings SET reaction_mode = ?, reaction_countdown = ? "
                    "WHERE singleton = 1",
                    (int(enabled), random.randint(15, 25) if enabled else None),
                )

            self._transaction(conn, action)

        await self._run(operation)

    async def admin_override(self, user_id: int) -> bool | None:
        """Return a stored admin decision, or None to use the environment list."""
        user_text = str(int(user_id))

        def operation() -> bool | None:
            conn = self._conn()
            self._settings(conn)
            row = conn.execute(
                "SELECT enabled FROM admin_overrides WHERE user_id = ?", (user_text,)
            ).fetchone()
            return None if row is None else bool(row["enabled"])

        return await self._run(operation)

    async def admin_overrides(self) -> dict[int, bool]:
        """Return all stored decisions for the private admin list."""

        def operation() -> dict[int, bool]:
            conn = self._conn()
            self._settings(conn)
            return {
                int(row["user_id"]): bool(row["enabled"])
                for row in conn.execute("SELECT user_id, enabled FROM admin_overrides")
            }

        return await self._run(operation)

    async def set_admin_override(self, user_id: int, enabled: bool) -> None:
        """Persist an owner's grant or revocation across restarts."""
        if isinstance(user_id, bool) or int(user_id) <= 0:
            raise ValueError("user_id must be a positive Discord ID")
        user_text = str(int(user_id))

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                self._settings(conn)
                conn.execute(
                    "INSERT INTO admin_overrides(user_id, enabled) VALUES (?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled",
                    (user_text, int(enabled)),
                )

            self._transaction(conn, action)

        await self._run(operation)

    async def set_paused(self, paused: bool, actor_id: int, now: float) -> None:
        """Persist collection pause; command authorization happens before this call."""
        now = self._timestamp(now)
        actor_text = str(int(actor_id))

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                self._settings(conn)

                if paused:
                    segment = self._open_segment(conn)
                    if segment is not None:
                        end = self._close_voice_segment(conn, segment, now)
                        self._finish_visit(
                            conn,
                            int(segment["visit_id"]),
                            end,
                            complete_end=False,
                        )
                    coverage = self._open_coverage(conn)
                    if coverage is not None:
                        end = max(float(coverage["checkpoint"]), now)
                        conn.execute(
                            "UPDATE coverage_intervals SET ended_at = ?, checkpoint = ? "
                            "WHERE interval_id = ?",
                            (end, end, int(coverage["interval_id"])),
                        )
                    self._close_open_gap(conn, now)
                    conn.execute(
                        "UPDATE settings SET paused = 1, paused_by = ?, connected = 0, "
                        "last_checkpoint = ?, resume_boundary = ? WHERE singleton = 1",
                        (actor_text, now, now),
                    )
                else:
                    conn.execute(
                        "UPDATE settings SET paused = 0, paused_by = NULL, connected = 0, "
                        "resume_boundary = ? WHERE singleton = 1",
                        (now,),
                    )

            self._transaction(conn, action)

        await self._run(operation)

    async def add_message(self, message_id: int, channel_id: int, created_at: float) -> bool:
        """Insert one message event, returning False for filtered or duplicate events."""
        inserted, _ = await self._add_message(message_id, channel_id, created_at, ordinary=False)
        return inserted

    async def add_message_with_reaction(
        self, message_id: int, channel_id: int, created_at: float, *, ordinary: bool
    ) -> tuple[bool, bool]:
        """Insert a message and advance the reaction interval in one transaction."""
        return await self._add_message(message_id, channel_id, created_at, ordinary=ordinary)

    async def _add_message(
        self, message_id: int, channel_id: int, created_at: float, *, ordinary: bool
    ) -> tuple[bool, bool]:
        created_at = self._timestamp(created_at)
        message_text = str(int(message_id))
        channel_text = str(int(channel_id))
        day = local_day(created_at, self.timezone)

        def operation() -> tuple[bool, bool]:
            conn = self._conn()

            def action() -> tuple[bool, bool]:
                settings = self._settings(conn)
                if bool(settings["paused"]):
                    return False, False
                if created_at < max(
                    float(settings["tracking_since"]), float(settings["resume_boundary"])
                ):
                    return False, False
                pruned_before = settings["pruned_before"]
                if pruned_before is not None and created_at < float(pruned_before):
                    return False, False
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO messages(message_id, channel_id, created_at, day) "
                    "VALUES (?, ?, ?, ?)",
                    (message_text, channel_text, created_at, day),
                )
                if cursor.rowcount == 0:
                    return False, False
                self._daily_delta(conn, day, messages=1)
                self._update_busiest_day(conn, day)
                if not ordinary or not settings["reaction_mode"]:
                    return True, False
                remaining = settings["reaction_countdown"]
                if remaining is None:
                    remaining = random.randint(15, 25)
                due = remaining <= 1
                conn.execute(
                    "UPDATE settings SET reaction_countdown = ? WHERE singleton = 1",
                    (random.randint(15, 25) if due else remaining - 1,),
                )
                return True, due

            return self._transaction(conn, action)

        return await self._run(operation)

    async def connect(self, now: float) -> None:
        """Start a fresh observed coverage interval unless collection is paused."""
        now = self._timestamp(now)

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                settings = self._settings(conn)
                if bool(settings["paused"]):
                    return
                if self._open_coverage(conn) is not None:
                    conn.execute(
                        "UPDATE settings SET connected = 1 WHERE singleton = 1"
                    )
                    return
                self._close_open_gap(conn, now)
                conn.execute(
                    "INSERT INTO coverage_intervals(started_at, checkpoint) VALUES (?, ?)",
                    (now, now),
                )
                conn.execute(
                    "UPDATE settings SET connected = 1, last_checkpoint = ? WHERE singleton = 1",
                    (now,),
                )

            self._transaction(conn, action)

        await self._run(operation)

    async def checkpoint(self, now: float) -> None:
        """Persist observed voice time and advance the reliable coverage checkpoint."""
        now = self._timestamp(now)

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                settings = self._settings(conn)
                if bool(settings["paused"]) or not bool(settings["connected"]):
                    return
                coverage = self._open_coverage(conn)
                if coverage is None:
                    return
                at = max(now, float(coverage["checkpoint"]))
                segment = self._open_segment(conn)
                if segment is not None:
                    at = max(at, float(segment["checkpoint"]))
                    self._add_voice_time(
                        conn,
                        int(segment["visit_id"]),
                        float(segment["checkpoint"]),
                        at,
                    )
                    conn.execute(
                        "UPDATE voice_segments SET checkpoint = ? WHERE segment_id = ?",
                        (at, int(segment["segment_id"])),
                    )
                    self._record_last_voice(conn, str(segment["channel_id"]), at)
                conn.execute(
                    "UPDATE coverage_intervals SET checkpoint = ? WHERE interval_id = ?",
                    (at, int(coverage["interval_id"])),
                )
                conn.execute(
                    "UPDATE settings SET last_checkpoint = ? WHERE singleton = 1", (at,)
                )

            self._transaction(conn, action)

        await self._run(operation)

    async def companion_transition(
        self, channel_id: int, member_id: int, joined: bool, now: float
    ) -> None:
        """Update a peer roster at an observed join or leave in Leland's channel."""
        now = self._timestamp(now)
        channel_text = str(int(channel_id))
        member_text = str(int(member_id))

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                settings = self._settings(conn)
                if bool(settings["paused"]) or not bool(settings["connected"]):
                    return
                segment = self._open_segment(conn)
                company = conn.execute(
                    "SELECT * FROM voice_company_current WHERE singleton = 1"
                ).fetchone()
                if (
                    segment is None or company is None
                    or str(segment["channel_id"]) != channel_text
                    or str(company["channel_id"]) != channel_text
                ):
                    return
                members = set(json.loads(company["member_ids"]))
                if (member_text in members) == joined:
                    return
                at = max(now, float(segment["checkpoint"]))
                self._add_voice_time(conn, int(segment["visit_id"]), float(segment["checkpoint"]), at)
                conn.execute(
                    "UPDATE voice_segments SET checkpoint = ? WHERE segment_id = ?",
                    (at, int(segment["segment_id"])),
                )
                self._record_last_voice(conn, channel_text, at)
                if joined:
                    members.add(member_text)
                else:
                    members.discard(member_text)
                conn.execute(
                    "UPDATE voice_company_current SET member_ids = ?, checkpoint = ? WHERE singleton = 1",
                    (json.dumps(sorted(members)), at),
                )
                coverage = self._open_coverage(conn)
                if coverage is not None:
                    conn.execute(
                        "UPDATE coverage_intervals SET checkpoint = ? WHERE interval_id = ?",
                        (at, int(coverage["interval_id"])),
                    )
                conn.execute("UPDATE settings SET last_checkpoint = ? WHERE singleton = 1", (at,))

            self._transaction(conn, action)

        await self._run(operation)

    async def voice_transition(
        self,
        channel_id: int | None,
        now: float,
        complete_start: bool = True,
        companions: set[int] | frozenset[int] = frozenset(),
    ) -> None:
        """Open, close, or move the active observed visit between tracked channels."""
        now = self._timestamp(now)
        channel_text = None if channel_id is None else str(int(channel_id))

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                settings = self._settings(conn)
                if bool(settings["paused"]) or not bool(settings["connected"]):
                    return
                segment = self._open_segment(conn)
                current_channel = None if segment is None else str(segment["channel_id"])
                if current_channel == channel_text:
                    return
                coverage = self._open_coverage(conn)
                at = now
                if coverage is not None:
                    at = max(at, float(coverage["checkpoint"]))
                if segment is not None:
                    at = max(at, float(segment["checkpoint"]))
                    close_at = self._close_voice_segment(conn, segment, at)
                    visit_id = int(segment["visit_id"])
                    if channel_text is None:
                        self._finish_visit(conn, visit_id, close_at, complete_end=True)
                else:
                    visit_id = -1

                if channel_text is not None:
                    previous = None
                    if segment is None and not complete_start:
                        previous = conn.execute(
                            "SELECT * FROM voice_visits ORDER BY visit_id DESC LIMIT 1"
                        ).fetchone()
                    if previous is not None and self._bridges(conn, previous, channel_text, at):
                        visit_id = int(previous["visit_id"])
                        conn.execute(
                            "UPDATE voice_visits SET ended_at = NULL, complete_end = 0 "
                            "WHERE visit_id = ?",
                            (visit_id,),
                        )
                    elif segment is None or current_channel is None:
                        cursor = conn.execute(
                            """INSERT INTO voice_visits(
                                   started_at, complete_start, complete_end, observed_seconds
                               ) VALUES (?, ?, 0, 0)""",
                            (at, int(complete_start)),
                        )
                        visit_id = int(cursor.lastrowid)
                        if complete_start:
                            self._daily_delta(
                                conn,
                                local_day(at, self.timezone),
                                voice_visits=1,
                            )
                    conn.execute(
                        """INSERT INTO voice_segments(
                               visit_id, channel_id, started_at, checkpoint
                           ) VALUES (?, ?, ?, ?)""",
                        (visit_id, channel_text, at, at),
                    )
                    members = sorted({str(int(member)) for member in companions if int(member) > 0})
                    conn.execute(
                        """INSERT INTO voice_company_current(singleton, channel_id, member_ids, checkpoint)
                           VALUES (1, ?, ?, ?)""",
                        (channel_text, json.dumps(members), at),
                    )
                    self._record_last_voice(conn, channel_text, at)

                if coverage is not None:
                    conn.execute(
                        "UPDATE coverage_intervals SET checkpoint = ? WHERE interval_id = ?",
                        (at, int(coverage["interval_id"])),
                    )
                conn.execute(
                    "UPDATE settings SET last_checkpoint = ? WHERE singleton = 1", (at,)
                )

            self._transaction(conn, action)

        await self._run(operation)

    async def disconnect(self, now: float) -> None:
        """Close observed state at last checkpoint and record uncertain time as a gap."""
        now = self._timestamp(now)

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                settings = self._settings(conn)
                coverage = self._open_coverage(conn)
                segment = self._open_segment(conn)
                if coverage is None and segment is None and not bool(settings["connected"]):
                    return

                checkpoint = (
                    float(coverage["checkpoint"])
                    if coverage is not None
                    else float(settings["last_checkpoint"] or now)
                )
                if segment is not None:
                    voice_checkpoint = float(segment["checkpoint"])
                    conn.execute(
                        "UPDATE voice_segments SET ended_at = ?, checkpoint = ? "
                        "WHERE segment_id = ?",
                        (voice_checkpoint, voice_checkpoint, int(segment["segment_id"])),
                    )
                    self._finish_visit(
                        conn,
                        int(segment["visit_id"]),
                        voice_checkpoint,
                        complete_end=False,
                    )
                    conn.execute("DELETE FROM voice_company_current")
                    checkpoint = min(checkpoint, voice_checkpoint)
                if coverage is not None:
                    coverage_checkpoint = float(coverage["checkpoint"])
                    conn.execute(
                        "UPDATE coverage_intervals SET ended_at = ? WHERE interval_id = ?",
                        (coverage_checkpoint, int(coverage["interval_id"])),
                    )
                    checkpoint = min(checkpoint, coverage_checkpoint)

                if not bool(settings["paused"]):
                    self._start_gap(conn, checkpoint, "disconnect")
                conn.execute(
                    "UPDATE settings SET connected = 0, last_checkpoint = ? WHERE singleton = 1",
                    (checkpoint,),
                )

            self._transaction(conn, action)

        await self._run(operation)

    async def stats(self, period: str, now: float, *, include_live: bool = True) -> dict[str, Any]:
        """Return aggregate message, voice, active-day, visit, and gap totals."""
        now = self._timestamp(now)

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            start, end = period_bounds(
                period,
                now,
                self.timezone,
                float(settings["tracking_since"]),
            )
            first_day = local_day(start, self.timezone)
            last_day = local_day(end, self.timezone)
            aggregate = conn.execute(
                """SELECT COALESCE(SUM(messages), 0) AS messages,
                          COALESCE(SUM(voice_seconds), 0) AS voice_seconds,
                          COALESCE(SUM(voice_visits), 0) AS voice_visits,
                          COALESCE(SUM(active), 0) AS active_days
                   FROM daily_stats WHERE day >= ? AND day <= ?""",
                (first_day, last_day),
            ).fetchone()

            voice_seconds = float(aggregate["voice_seconds"])
            active_dates = {
                str(row["day"])
                for row in conn.execute(
                    "SELECT day FROM daily_stats WHERE day >= ? AND day <= ? AND active = 1",
                    (first_day, last_day),
                )
            }
            if include_live and bool(settings["connected"]) and not bool(settings["paused"]):
                segment = self._open_segment(conn)
                if segment is not None:
                    live_start = float(segment["checkpoint"])
                    live_end = max(now, live_start)
                    clipped_start = max(live_start, start)
                    clipped_end = min(live_end, end)
                    if clipped_end > clipped_start:
                        voice_seconds += clipped_end - clipped_start
                        active_dates.update(
                            day
                            for day, seconds in split_interval_by_day(
                                clipped_start, clipped_end, self.timezone
                            )
                            if seconds > 0
                        )

            gap_seconds = 0.0
            for row in conn.execute(
                "SELECT started_at, ended_at FROM coverage_gaps WHERE started_at < ? "
                "AND COALESCE(ended_at, ?) > ?",
                (end, end, start),
            ):
                gap_seconds += interval_overlap(
                    float(row["started_at"]),
                    end if row["ended_at"] is None else float(row["ended_at"]),
                    start,
                    end,
                )

            return {
                "messages": int(aggregate["messages"]),
                "voice_seconds": voice_seconds,
                "voice_visits": int(aggregate["voice_visits"]),
                "active_days": len(active_dates),
                "tracking_since": float(settings["tracking_since"]),
                "gap_seconds": gap_seconds,
                "paused": bool(settings["paused"]),
            }

        return await self._run(operation)

    async def message_channel_counts(self, period: str, now: float) -> dict[int, int]:
        """Return stored target message counts by channel within a period."""
        now = self._timestamp(now)

        def operation() -> dict[int, int]:
            conn = self._conn()
            settings = self._settings(conn)
            start, end = period_bounds(
                period,
                now,
                self.timezone,
                float(settings["tracking_since"]),
            )
            return {
                int(row["channel_id"]): int(row["count"])
                for row in conn.execute(
                    "SELECT channel_id, COUNT(*) AS count FROM messages "
                    "WHERE created_at >= ? AND created_at <= ? GROUP BY channel_id",
                    (start, end),
                )
            }

        return await self._run(operation)

    async def latest_messages(
        self, period: str, now: float, channel_ids: Collection[int], limit: int
    ) -> list[dict[str, Any]]:
        """Return newest-first message metadata in the given channels and period."""
        now = self._timestamp(now)
        channels = [str(channel_id) for channel_id in channel_ids]
        if not channels or limit <= 0:
            return []

        def operation() -> list[dict[str, Any]]:
            conn = self._conn()
            settings = self._settings(conn)
            start, end = period_bounds(
                period,
                now,
                self.timezone,
                float(settings["tracking_since"]),
            )
            placeholders = ", ".join("?" for _ in channels)
            return [
                {
                    "message_id": int(row["message_id"]),
                    "channel_id": int(row["channel_id"]),
                    "created_at": float(row["created_at"]),
                }
                for row in conn.execute(
                    "SELECT message_id, channel_id, created_at FROM messages "
                    "WHERE created_at >= ? AND created_at <= ? "
                    f"AND channel_id IN ({placeholders}) "
                    "ORDER BY created_at DESC, CAST(message_id AS INTEGER) DESC LIMIT ?",
                    (start, end, *channels, limit),
                )
            ]

        return await self._run(operation)

    @staticmethod
    def _is_live(settings: sqlite3.Row, include_live: bool) -> bool:
        """True when open voice and coverage may be reported through now."""
        return include_live and bool(settings["connected"]) and not bool(settings["paused"])

    def _live_segment(
        self, conn: sqlite3.Connection, settings: sqlite3.Row, include_live: bool
    ) -> sqlite3.Row | None:
        """Return the open voice segment when live time may be reported."""
        return self._open_segment(conn) if self._is_live(settings, include_live) else None

    @staticmethod
    def _detail_since(settings: sqlite3.Row, start: float) -> float:
        """Return where retained message and voice detail begins at or after ``start``.

        Deletion also moves ``pruned_before``, but only pruning past the
        tracking start means retained detail is missing.
        """
        pruned = settings["pruned_before"]
        if pruned is None or float(pruned) <= float(settings["tracking_since"]):
            return start
        return max(start, float(pruned))

    def _watched_by_day(
        self, conn: sqlite3.Connection, start: float, end: float, live_until: float | None = None
    ) -> dict[str, float]:
        """Return seconds of collector coverage per local day inside ``[start, end)``.

        An open interval counts through its checkpoint, or through
        ``live_until`` when the collector is currently reliable.
        """
        watched: dict[str, float] = {}
        for row in conn.execute(
            "SELECT started_at, ended_at, checkpoint FROM coverage_intervals "
            "WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?)",
            (end, start),
        ):
            if row["ended_at"] is not None:
                interval_end = float(row["ended_at"])
            elif live_until is not None:
                interval_end = max(float(row["checkpoint"]), live_until)
            else:
                interval_end = float(row["checkpoint"])
            piece_start = max(start, float(row["started_at"]))
            piece_end = min(end, interval_end)
            for day, seconds in split_interval_by_day(piece_start, piece_end, self.timezone):
                watched[day] = watched.get(day, 0.0) + seconds
        return watched

    def _window_totals(
        self,
        conn: sqlite3.Connection,
        settings: sqlite3.Row,
        start: float,
        end: float,
        now: float,
        include_live: bool,
    ) -> dict[str, Any]:
        """Return exact detail-based totals for ``[start, end)``."""
        messages = int(conn.execute(
            "SELECT COUNT(*) FROM messages WHERE created_at >= ? AND created_at < ?",
            (start, end),
        ).fetchone()[0])
        visits = int(conn.execute(
            "SELECT COUNT(*) FROM voice_visits WHERE complete_start = 1 "
            "AND started_at >= ? AND started_at < ?",
            (start, end),
        ).fetchone()[0])
        voice = 0.0
        for row in conn.execute(
            "SELECT started_at, COALESCE(ended_at, checkpoint) AS ended_at FROM voice_segments "
            "WHERE started_at < ? AND COALESCE(ended_at, checkpoint) > ?",
            (end, start),
        ):
            voice += interval_overlap(float(row["started_at"]), float(row["ended_at"]), start, end)
        segment = self._live_segment(conn, settings, include_live)
        if segment is not None:
            voice += interval_overlap(float(segment["checkpoint"]), max(now, float(segment["checkpoint"])), start, end)
        live_until = now if self._is_live(settings, include_live) else None
        watched = sum(self._watched_by_day(conn, start, end, live_until).values())
        return {
            "start": start,
            "end": end,
            "messages": messages,
            "voice_seconds": voice,
            "voice_visits": visits,
            "watched_seconds": watched,
        }

    async def period_comparison(
        self, period: str, now: float, *, include_live: bool = True
    ) -> dict[str, Any]:
        """Compare a period so far with the previous period up to the same point.

        Both windows use retained detail so partial days compare fairly.
        ``previous`` is ``None`` with a ``reason`` of ``all``, ``untracked``, or
        ``pruned`` when no honest comparison exists.
        """
        now = self._timestamp(now)

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            tracking_since = float(settings["tracking_since"])
            start, end = period_bounds(period, now, self.timezone, tracking_since)
            current = self._window_totals(conn, settings, start, end, now, include_live)
            previous_bounds = previous_period_bounds(period, now, self.timezone)
            result: dict[str, Any] = {"current": current, "previous": None, "reason": None}
            if previous_bounds is None:
                result["reason"] = "all"
            elif previous_bounds[0] < tracking_since:
                result["reason"] = "untracked"
            elif self._detail_since(settings, previous_bounds[0]) > previous_bounds[0]:
                result["reason"] = "pruned"
            else:
                result["previous"] = self._window_totals(
                    conn, settings, *previous_bounds, now, include_live
                )
            return result

        return await self._run(operation)

    async def company_daily(
        self, period: str, now: float, *, include_live: bool = True
    ) -> list[dict[str, Any]]:
        """Return voice seconds by local day, channel, and peer (0 means alone).

        ``seconds`` splits each shared second evenly among the peers present;
        ``full_seconds`` credits every peer with the whole second.
        """
        now = self._timestamp(now)

        def operation() -> list[dict[str, Any]]:
            conn = self._conn()
            settings = self._settings(conn)
            start, end = period_bounds(
                period, now, self.timezone, float(settings["tracking_since"])
            )
            totals: dict[tuple[str, int, int], tuple[float, float]] = {}
            for row in conn.execute(
                """SELECT day, channel_id, member_id, seconds, full_seconds FROM voice_company_daily
                   WHERE day >= ? AND day <= ?""",
                (local_day(start, self.timezone), local_day(end, self.timezone)),
            ):
                key = (str(row["day"]), int(row["channel_id"]), int(row["member_id"]))
                totals[key] = (float(row["seconds"]), float(row["full_seconds"]))
            segment = self._live_segment(conn, settings, include_live)
            company = conn.execute(
                "SELECT * FROM voice_company_current WHERE singleton = 1"
            ).fetchone()
            if (
                segment is not None and company is not None
                and segment["channel_id"] == company["channel_id"]
            ):
                live_start = max(float(segment["checkpoint"]), start)
                members = json.loads(company["member_ids"]) or ["0"]
                for day, seconds in split_interval_by_day(live_start, end, self.timezone):
                    for member_id in members:
                        key = (day, int(company["channel_id"]), int(member_id))
                        split, full = totals.get(key, (0.0, 0.0))
                        totals[key] = (split + seconds / len(members), full + seconds)
            return [
                {
                    "day": day, "channel_id": channel_id, "member_id": member_id,
                    "seconds": split, "full_seconds": full,
                }
                for (day, channel_id, member_id), (split, full) in sorted(totals.items())
            ]

        return await self._run(operation)

    async def company_totals(
        self, period: str, now: float, *, include_live: bool = True
    ) -> list[dict[str, Any]]:
        """Return split and full voice seconds by channel and peer (0 means alone)."""
        totals: dict[tuple[int, int], tuple[float, float]] = {}
        for row in await self.company_daily(period, now, include_live=include_live):
            key = (row["channel_id"], row["member_id"])
            split, full = totals.get(key, (0.0, 0.0))
            totals[key] = (split + row["seconds"], full + row["full_seconds"])
        return [
            {"channel_id": channel_id, "member_id": member_id, "seconds": split, "full_seconds": full}
            for (channel_id, member_id), (split, full) in sorted(totals.items())
        ]

    async def daily_trend(
        self, period: str, now: float, *, include_live: bool = True
    ) -> list[dict[str, Any]]:
        """Return message, voice, and visit totals for every local day in a period.

        ``watched`` is true only for a finished day the collector covered in
        full, so a day without activity can be called quiet rather than unknown.
        """
        now = self._timestamp(now)

        def operation() -> list[dict[str, Any]]:
            conn = self._conn()
            settings = self._settings(conn)
            tracking_since = float(settings["tracking_since"])
            start, end = period_bounds(period, now, self.timezone, tracking_since)
            # Days before tracking began are unknown, not quiet, so leave them out.
            start = max(start, tracking_since)
            first_day = local_date(start, self.timezone)
            last_day = local_date(end, self.timezone)
            watched = self._watched_by_day(
                conn, local_midnight(first_day, self.timezone), end
            )
            series: dict[str, dict[str, Any]] = {}
            day = first_day
            while day <= last_day:
                day_start = local_midnight(day, self.timezone)
                day_end = local_midnight(day + timedelta(days=1), self.timezone)
                series[day.isoformat()] = {
                    "day": day.isoformat(), "messages": 0, "voice_seconds": 0.0, "voice_visits": 0,
                    # One second of slack absorbs float rounding at interval joins.
                    "watched": day_end <= now
                    and watched.get(day.isoformat(), 0.0) >= day_end - day_start - 1.0,
                }
                day += timedelta(days=1)
            for row in conn.execute(
                """SELECT day, messages, voice_seconds, voice_visits FROM daily_stats
                   WHERE day >= ? AND day <= ?""",
                (first_day.isoformat(), last_day.isoformat()),
            ):
                entry = series.get(str(row["day"]))
                if entry is not None:
                    entry["messages"] = int(row["messages"])
                    entry["voice_seconds"] = float(row["voice_seconds"])
                    entry["voice_visits"] = int(row["voice_visits"])
            segment = self._live_segment(conn, settings, include_live)
            if segment is not None:
                live_start = max(float(segment["checkpoint"]), start)
                for piece_day, seconds in split_interval_by_day(live_start, end, self.timezone):
                    if piece_day in series:
                        series[piece_day]["voice_seconds"] += seconds
            return list(series.values())

        return await self._run(operation)

    async def message_times(self, period: str, now: float) -> dict[str, Any]:
        """Return retained message send times in a period, oldest first.

        ``since`` is where retained message detail begins inside the period;
        older messages survive only in daily totals and have no send time.
        """
        now = self._timestamp(now)

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            start, end = period_bounds(
                period, now, self.timezone, float(settings["tracking_since"])
            )
            times = [
                float(row[0])
                for row in conn.execute(
                    "SELECT created_at FROM messages WHERE created_at >= ? AND created_at <= ? "
                    "ORDER BY created_at",
                    (start, end),
                )
            ]
            return {"times": times, "since": self._detail_since(settings, start), "period_start": start}

        return await self._run(operation)

    async def voice_hours(
        self, period: str, now: float, *, include_live: bool = True
    ) -> dict[str, Any]:
        """Return retained observed voice seconds by local hour of day in a period."""
        now = self._timestamp(now)

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            start, end = period_bounds(
                period, now, self.timezone, float(settings["tracking_since"])
            )
            spans = [
                (float(row["started_at"]), float(row["ended_at"]))
                for row in conn.execute(
                    "SELECT started_at, COALESCE(ended_at, checkpoint) AS ended_at "
                    "FROM voice_segments WHERE started_at < ? AND COALESCE(ended_at, checkpoint) > ?",
                    (end, start),
                )
            ]
            segment = self._live_segment(conn, settings, include_live)
            if segment is not None:
                spans.append((float(segment["checkpoint"]), max(now, float(segment["checkpoint"]))))
            # Clip to retained detail so voice covers the same window as message times.
            since = self._detail_since(settings, start)
            hours = [0.0] * 24
            for span_start, span_end in spans:
                for hour, seconds in split_interval_by_hour(
                    max(since, span_start), min(end, span_end), self.timezone
                ):
                    hours[hour] += seconds
            return {"hours": hours, "since": since, "period_start": start}

        return await self._run(operation)

    async def records(self, now: float, *, include_live: bool = True) -> dict[str, Any]:
        """Return the busiest message day, longest fully observed voice visit,
        and the observed length so far of a live visit."""
        now = self._timestamp(now)

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            current_seconds: float | None = None
            current_complete_start = False
            segment = self._live_segment(conn, settings, include_live)
            if segment is not None:
                visit = conn.execute(
                    "SELECT observed_seconds, complete_start FROM voice_visits WHERE visit_id = ?",
                    (int(segment["visit_id"]),),
                ).fetchone()
                if visit is not None:
                    current_seconds = float(visit["observed_seconds"]) + max(
                        0.0, now - float(segment["checkpoint"])
                    )
                    current_complete_start = bool(visit["complete_start"])
            busiest = conn.execute(
                "SELECT value, at FROM records WHERE record_type = 'busiest_day'"
            ).fetchone()
            longest = conn.execute(
                "SELECT value, at FROM records WHERE record_type = 'longest_visit'"
            ).fetchone()
            return {
                "busiest_day": None if busiest is None else str(busiest["at"]),
                "busiest_day_messages": 0 if busiest is None else int(busiest["value"]),
                "longest_visit_seconds": 0.0 if longest is None else float(longest["value"]),
                "longest_visit_at": (
                    None if longest is None else float(longest["at"])
                ),
                "current_visit_seconds": current_seconds,
                "current_visit_complete_start": current_complete_start,
            }

        return await self._run(operation)

    async def last_voice(self, now: float, *, include_live: bool = True) -> dict[str, Any] | None:
        """Return the latest observed voice channel and time, or no observation."""
        now = self._timestamp(now)

        def operation() -> dict[str, Any] | None:
            conn = self._conn()
            settings = self._settings(conn)
            if include_live and bool(settings["connected"]) and not bool(settings["paused"]):
                segment = self._open_segment(conn)
                if segment is not None:
                    return {
                        "channel_id": int(segment["channel_id"]),
                        "seen_at": max(now, float(segment["checkpoint"])),
                        "current": True,
                        "observed_since": float(segment["started_at"]),
                    }
            row = conn.execute("SELECT channel_id, seen_at FROM last_voice WHERE singleton = 1").fetchone()
            if row is None:
                return None
            return {
                "channel_id": int(row["channel_id"]),
                "seen_at": float(row["seen_at"]),
                "current": False,
                "observed_since": None,
            }

        return await self._run(operation)

    async def delete_data(self, actor_id: int, now: float) -> None:
        """Erase target statistics and all managed backups, then pause collection."""
        now = self._timestamp(now)

        def operation() -> None:
            conn = self._conn()
            self._settings(conn)
            actor_text = str(int(actor_id))

            # Remove backups first. If a managed backup cannot be erased, keep the
            # database intact and report the failure instead of claiming deletion.
            self._remove_managed_backups()

            def action() -> None:
                conn.execute("DELETE FROM voice_company_current")
                conn.execute("DELETE FROM voice_company_daily")
                conn.execute("DELETE FROM voice_visits")
                conn.execute("DELETE FROM voice_segments")
                conn.execute("DELETE FROM messages")
                conn.execute("DELETE FROM daily_stats")
                conn.execute("DELETE FROM coverage_intervals")
                conn.execute("DELETE FROM coverage_gaps")
                conn.execute("DELETE FROM records")
                conn.execute("DELETE FROM last_voice")
                conn.execute(
                    """UPDATE settings SET tracking_since = ?, paused = 1, evil_mode = 0,
                           reaction_mode = 0, reaction_countdown = NULL, paused_by = ?,
                           connected = 0, last_checkpoint = NULL, resume_boundary = ?,
                           pruned_before = ? WHERE singleton = 1""",
                    (now, actor_text, now, now),
                )

            self._transaction(conn, action)
            # Logical deletion is committed at this point. Compaction only
            # reclaims space; failure must not tell the caller that their data
            # still exists in the application or invite another destructive retry.
            try:
                self._compact_after_deletion(conn)
            except sqlite3.Error:
                logger.exception("Data deletion committed, but database compaction failed")

        await self._run(operation)

    @staticmethod
    def _compact_after_deletion(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")

    def _remove_managed_backups(self) -> None:
        if not self.backup_dir.exists():
            return
        for path in self.backup_dir.iterdir():
            if (
                path.is_file() or path.is_symlink()
            ) and _is_managed_backup(path.name):
                path.unlink()

    def _write_backup(self, conn: sqlite3.Connection, now: float) -> Path:
        day = local_day(now, self.timezone)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = self.backup_dir / f"flock-cctv-{day}.sqlite3"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".flock-cctv-{day}.", suffix=".sqlite3.tmp", dir=self.backup_dir
        )
        os.close(fd)
        temp = Path(temp_name)
        try:
            destination = sqlite3.connect(str(temp))
            try:
                conn.backup(destination)
            finally:
                destination.close()
            os.replace(temp, target)
        except BaseException:
            try:
                temp.unlink(missing_ok=True)
            finally:
                raise

        backups: list[tuple[date, Path]] = []
        for path in self.backup_dir.iterdir():
            match = _BACKUP_RE.fullmatch(path.name)
            if match is None:
                continue
            try:
                backup_day = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            backups.append((backup_day, path))
        backups.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        for _, stale in backups[7:]:
            stale.unlink()
        return target

    async def maintenance(self, now: float, retention_days: int) -> None:
        """Prune old detail, retain daily aggregates, and make a seven-day backup set."""
        now = self._timestamp(now)
        if isinstance(retention_days, bool) or int(retention_days) <= 0:
            raise ValueError("retention_days must be positive")
        retention_days = int(retention_days)
        cutoff = now - retention_days * 86400.0

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                settings = self._settings(conn)
                old_pruned = settings["pruned_before"]
                effective_cutoff = cutoff if old_pruned is None else max(
                    cutoff, float(old_pruned)
                )
                conn.execute("DELETE FROM messages WHERE created_at < ?", (effective_cutoff,))
                conn.execute(
                    "DELETE FROM voice_visits WHERE ended_at IS NOT NULL AND ended_at < ?",
                    (effective_cutoff,),
                )
                conn.execute(
                    "UPDATE settings SET pruned_before = ?, retention_days = ? "
                    "WHERE singleton = 1",
                    (effective_cutoff, retention_days),
                )

            self._transaction(conn, action)
            # The SQLite backup API captures a consistent snapshot after pruning.
            self._write_backup(conn, now)

        await self._run(operation)


__all__ = ["Store", "StoreError"]
