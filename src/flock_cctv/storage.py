"""Serialized asynchronous SQLite storage for Flock CCTV.

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
from typing import Any, Callable, TypeVar

from .stats import (
    get_timezone,
    intersect_intervals,
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

    SCHEMA_VERSION = 1
    DEFAULT_RETENTION_DAYS = 90
    # A tracker reconnect or restart closes each open visit at its last
    # checkpoint. If that person is found in the same channel this soon
    # afterwards, the visit continues instead of splitting into two incomplete
    # visits. The uncovered interval is still a coverage gap and is never counted.
    VISIT_BRIDGE_SECONDS = 120.0

    def __init__(self, path: Path, backup_dir: Path, timezone: str) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir)
        self.timezone = timezone
        get_timezone(timezone)  # Fail early for invalid IANA names.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="flock-sqlite")
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
        # A single-target Leland database shares table names with this schema but
        # not their shape; refuse it clearly instead of failing on a missing column.
        settings_columns = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
        if "target_user_id" in settings_columns:
            raise StoreError(
                "database uses the single-target Leland schema; "
                "convert it with the one-time legacy import tool"
            )
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

            CREATE TABLE IF NOT EXISTS tracked_users (
                user_id TEXT PRIMARY KEY,
                tracking_since REAL NOT NULL,
                active INTEGER NOT NULL CHECK (active IN (0, 1)),
                added_by TEXT,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tracking_intervals (
                interval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_tracking_interval_idx
                ON tracking_intervals(user_id) WHERE ended_at IS NULL;

            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                day TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_user_created_idx
                ON messages(user_id, created_at);
            CREATE INDEX IF NOT EXISTS messages_created_at_idx ON messages(created_at);

            CREATE TABLE IF NOT EXISTS daily_stats (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                messages INTEGER NOT NULL DEFAULT 0,
                voice_seconds REAL NOT NULL DEFAULT 0,
                voice_visits INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
                PRIMARY KEY (user_id, day)
            );

            CREATE TABLE IF NOT EXISTS voice_visits (
                visit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                complete_start INTEGER NOT NULL CHECK (complete_start IN (0, 1)),
                complete_end INTEGER NOT NULL DEFAULT 0 CHECK (complete_end IN (0, 1)),
                observed_seconds REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS voice_visits_user_started_idx
                ON voice_visits(user_id, started_at);

            CREATE TABLE IF NOT EXISTS voice_segments (
                segment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                visit_id INTEGER NOT NULL REFERENCES voice_visits(visit_id) ON DELETE CASCADE,
                user_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                checkpoint REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS voice_segments_visit_idx ON voice_segments(visit_id);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_voice_segment_idx
                ON voice_segments(user_id) WHERE ended_at IS NULL;

            CREATE TABLE IF NOT EXISTS voice_company_current (
                user_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                member_ids TEXT NOT NULL,
                checkpoint REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_company_daily (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                seconds REAL NOT NULL,
                full_seconds REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day, channel_id, member_id)
            );

            CREATE TABLE IF NOT EXISTS last_voice (
                user_id TEXT PRIMARY KEY,
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
                user_id TEXT NOT NULL,
                record_type TEXT NOT NULL,
                value REAL NOT NULL,
                at TEXT NOT NULL,
                PRIMARY KEY (user_id, record_type)
            );

            CREATE TABLE IF NOT EXISTS admin_overrides (
                user_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
            );
            """
        )
        if version < self.SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            conn.commit()

    @staticmethod
    def _daily_delta(
        conn: sqlite3.Connection,
        user_text: str,
        day: str,
        *,
        messages: int = 0,
        voice_seconds: float = 0.0,
        voice_visits: int = 0,
    ) -> None:
        conn.execute(
            """INSERT INTO daily_stats(user_id, day, messages, voice_seconds, voice_visits, active)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, day) DO UPDATE SET
                   messages = daily_stats.messages + excluded.messages,
                   voice_seconds = daily_stats.voice_seconds + excluded.voice_seconds,
                   voice_visits = daily_stats.voice_visits + excluded.voice_visits,
                   active = CASE WHEN daily_stats.active = 1 OR excluded.active = 1
                                 THEN 1 ELSE 0 END""",
            (user_text, day, messages, voice_seconds, voice_visits,
             int(messages > 0 or voice_seconds > 0.0)),
        )

    def _add_voice_time(
        self, conn: sqlite3.Connection, user_text: str, visit_id: int, start: float, end: float
    ) -> float:
        if end <= start:
            return 0.0
        delta = end - start
        for day, seconds in split_interval_by_day(start, end, self.timezone):
            if seconds > 0:
                self._daily_delta(conn, user_text, day, voice_seconds=seconds)
        self._add_company_time(conn, user_text, start, end)
        conn.execute(
            "UPDATE voice_visits SET observed_seconds = observed_seconds + ? WHERE visit_id = ?",
            (delta, visit_id),
        )
        return delta

    def _add_company_time(
        self, conn: sqlite3.Connection, user_text: str, start: float, end: float
    ) -> None:
        row = conn.execute(
            "SELECT * FROM voice_company_current WHERE user_id = ?", (user_text,)
        ).fetchone()
        if row is None or end <= start:
            return
        members = json.loads(row["member_ids"])
        recipients = members if members else ["0"]  # 0 is the alone slice.
        for day, seconds in split_interval_by_day(start, end, self.timezone):
            for member_id in recipients:
                conn.execute(
                    """INSERT INTO voice_company_daily(
                           user_id, day, channel_id, member_id, seconds, full_seconds
                       ) VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(user_id, day, channel_id, member_id) DO UPDATE SET
                           seconds = seconds + excluded.seconds,
                           full_seconds = full_seconds + excluded.full_seconds""",
                    (user_text, day, row["channel_id"], member_id, seconds / len(recipients), seconds),
                )
        conn.execute(
            "UPDATE voice_company_current SET checkpoint = ? WHERE user_id = ?", (end, user_text)
        )

    @staticmethod
    def _open_segment(conn: sqlite3.Connection, user_text: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM voice_segments WHERE user_id = ? AND ended_at IS NULL "
            "ORDER BY segment_id DESC LIMIT 1",
            (user_text,),
        ).fetchone()

    @staticmethod
    def _open_segments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM voice_segments WHERE ended_at IS NULL ORDER BY segment_id"
        ).fetchall()

    @staticmethod
    def _open_coverage(conn: sqlite3.Connection) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM coverage_intervals WHERE ended_at IS NULL "
            "ORDER BY interval_id DESC LIMIT 1"
        ).fetchone()

    @staticmethod
    def _advance_checkpoint(conn: sqlite3.Connection, at: float) -> None:
        """Move the reliable-coverage checkpoint forward, never back.

        Several people's events share one coverage interval, so a person with
        a lagging segment must not pull the shared checkpoint behind another's.
        """
        conn.execute(
            "UPDATE coverage_intervals SET checkpoint = MAX(checkpoint, ?) "
            "WHERE ended_at IS NULL",
            (at,),
        )
        conn.execute(
            "UPDATE settings SET last_checkpoint = MAX(COALESCE(last_checkpoint, ?), ?) "
            "WHERE singleton = 1",
            (at, at),
        )

    @staticmethod
    def _record_last_voice(
        conn: sqlite3.Connection, user_text: str, channel_id: str, seen_at: float
    ) -> None:
        conn.execute(
            """INSERT INTO last_voice(user_id, channel_id, seen_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   channel_id = excluded.channel_id, seen_at = excluded.seen_at
               WHERE excluded.seen_at >= last_voice.seen_at""",
            (user_text, channel_id, seen_at),
        )

    def _close_voice_segment(
        self, conn: sqlite3.Connection, segment: sqlite3.Row, at: float
    ) -> float:
        user_text = str(segment["user_id"])
        close_at = max(float(segment["checkpoint"]), at)
        delta = self._add_voice_time(
            conn, user_text, int(segment["visit_id"]), float(segment["checkpoint"]), close_at
        )
        conn.execute(
            "UPDATE voice_segments SET ended_at = ?, checkpoint = ? WHERE segment_id = ?",
            (close_at, close_at, int(segment["segment_id"])),
        )
        conn.execute("DELETE FROM voice_company_current WHERE user_id = ?", (user_text,))
        self._record_last_voice(conn, user_text, str(segment["channel_id"]), close_at)
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
                str(row["user_id"]),
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

        ``previous`` is the same person's latest visit. Only a visit cut short
        by a disconnect or restart qualifies: a pause, a departure, an untrack
        and re-track, or an outage longer than the bridge window keeps the split.
        """
        if previous["ended_at"] is None or bool(previous["complete_end"]):
            return False
        ended_at = float(previous["ended_at"])
        if not 0 <= at - ended_at <= self.VISIT_BRIDGE_SECONDS:
            return False
        if self._visit_channel(conn, int(previous["visit_id"]), last=True) != channel_id:
            return False
        # The visit and its continuation must sit inside one tracking interval.
        if conn.execute(
            """SELECT 1 FROM tracking_intervals
               WHERE user_id = ? AND started_at <= ? AND (ended_at IS NULL OR ended_at > ?)
               LIMIT 1""",
            (str(previous["user_id"]), ended_at, at),
        ).fetchone() is None:
            return False
        return conn.execute(
            """SELECT 1 FROM coverage_gaps
               WHERE reason IN ('disconnect', 'process_restart')
                 AND started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)
               LIMIT 1""",
            (ended_at, at),
        ).fetchone() is not None

    def _recompute_longest_visit(self, conn: sqlite3.Connection, user_text: str) -> None:
        """Apply the bridge rule to one person's retained visits split before it existed."""
        rows = conn.execute(
            "SELECT * FROM voice_visits WHERE user_id = ? AND ended_at IS NOT NULL "
            "ORDER BY started_at, visit_id",
            (user_text,),
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
                self._update_longest_visit(
                    conn, user_text, seconds, float(chain_start["started_at"])
                )

    @staticmethod
    def _update_longest_visit(
        conn: sqlite3.Connection, user_text: str, duration: float, started_at: float
    ) -> None:
        current = conn.execute(
            "SELECT value FROM records WHERE user_id = ? AND record_type = 'longest_visit'",
            (user_text,),
        ).fetchone()
        if current is None or duration > float(current["value"]):
            conn.execute(
                "INSERT INTO records(user_id, record_type, value, at) "
                "VALUES (?, 'longest_visit', ?, ?) "
                "ON CONFLICT(user_id, record_type) DO UPDATE SET "
                "value = excluded.value, at = excluded.at",
                (user_text, duration, repr(started_at)),
            )

    def _update_busiest_day(self, conn: sqlite3.Connection, user_text: str, day: str) -> None:
        count_row = conn.execute(
            "SELECT messages FROM daily_stats WHERE user_id = ? AND day = ?", (user_text, day)
        ).fetchone()
        count = int(count_row["messages"])
        existing = conn.execute(
            "SELECT value, at FROM records WHERE user_id = ? AND record_type = 'busiest_day'",
            (user_text,),
        ).fetchone()
        if existing is None or count > int(existing["value"]) or (
            count == int(existing["value"]) and day < str(existing["at"])
        ):
            conn.execute(
                "INSERT INTO records(user_id, record_type, value, at) "
                "VALUES (?, 'busiest_day', ?, ?) "
                "ON CONFLICT(user_id, record_type) DO UPDATE SET "
                "value = excluded.value, at = excluded.at",
                (user_text, count, day),
            )

    async def initialize(self, now: float, guild_id: int) -> None:
        """Create or validate the database and recover any previous open session."""
        now = self._timestamp(now)
        guild_id_text = str(int(guild_id))

        def operation() -> None:
            conn = self._conn()
            self._create_schema(conn)

            def action() -> None:
                row = conn.execute("SELECT * FROM settings WHERE singleton = 1").fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO settings(
                               singleton, guild_id, timezone, tracking_since, resume_boundary
                           ) VALUES (1, ?, ?, ?, ?)""",
                        (guild_id_text, self.timezone, now, now),
                    )
                    return
                if row["guild_id"] != guild_id_text:
                    raise StoreError("database belongs to a different guild")
                if row["timezone"] != self.timezone:
                    raise StoreError("database timezone differs from configured timezone")

                was_connected = bool(row["connected"])
                intentionally_paused = bool(row["paused"])
                recovery_point: float | None = None
                # Every person's open segment ends at its own last checkpoint.
                for segment in self._open_segments(conn):
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
                for visitor in conn.execute("SELECT DISTINCT user_id FROM voice_visits").fetchall():
                    self._recompute_longest_visit(conn, str(visitor["user_id"]))

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
                    for segment in self._open_segments(conn):
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

    # -- Tracked list -------------------------------------------------------

    @staticmethod
    def _tracked_row(conn: sqlite3.Connection, user_text: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM tracked_users WHERE user_id = ?", (user_text,)
        ).fetchone()

    @staticmethod
    def _collection_start(conn: sqlite3.Connection, user_text: str) -> float | None:
        """Return when an active person's open tracking interval began, else None."""
        row = conn.execute(
            """SELECT i.started_at FROM tracking_intervals i
               JOIN tracked_users u ON u.user_id = i.user_id
               WHERE i.user_id = ? AND i.ended_at IS NULL AND u.active = 1""",
            (user_text,),
        ).fetchone()
        return None if row is None else float(row["started_at"])

    async def track_user(self, user_id: int, actor_id: int, now: float) -> bool:
        """Start tracking a person; False when they are already tracked."""
        now = self._timestamp(now)
        user_text = str(int(user_id))
        actor_text = str(int(actor_id))

        def operation() -> bool:
            conn = self._conn()

            def action() -> bool:
                self._settings(conn)
                row = self._tracked_row(conn, user_text)
                if row is not None and bool(row["active"]):
                    return False
                # Intervals never overlap, even if the clock stepped backwards.
                last_end = conn.execute(
                    "SELECT MAX(ended_at) FROM tracking_intervals WHERE user_id = ?",
                    (user_text,),
                ).fetchone()[0]
                started = now if last_end is None else max(now, float(last_end))
                if row is None:
                    conn.execute(
                        "INSERT INTO tracked_users(user_id, tracking_since, active, added_by, updated_at) "
                        "VALUES (?, ?, 1, ?, ?)",
                        (user_text, started, actor_text, now),
                    )
                else:
                    conn.execute(
                        "UPDATE tracked_users SET active = 1, added_by = ?, updated_at = ? "
                        "WHERE user_id = ?",
                        (actor_text, now, user_text),
                    )
                conn.execute(
                    "INSERT INTO tracking_intervals(user_id, started_at) VALUES (?, ?)",
                    (user_text, started),
                )
                return True

            return self._transaction(conn, action)

        return await self._run(operation)

    async def untrack_user(self, user_id: int, actor_id: int, now: float) -> bool:
        """Stop tracking a person, keeping their history; False when not tracked.

        An open visit ends at ``now`` as incomplete, exactly like a pause.
        """
        now = self._timestamp(now)
        user_text = str(int(user_id))
        int(actor_id)  # Validated for symmetry; removal does not store its actor.

        def operation() -> bool:
            conn = self._conn()

            def action() -> bool:
                self._settings(conn)
                row = self._tracked_row(conn, user_text)
                if row is None or not bool(row["active"]):
                    return False
                ended = now
                segment = self._open_segment(conn, user_text)
                if segment is not None:
                    ended = self._close_voice_segment(conn, segment, now)
                    self._finish_visit(
                        conn, int(segment["visit_id"]), ended, complete_end=False
                    )
                conn.execute(
                    "DELETE FROM voice_company_current WHERE user_id = ?", (user_text,)
                )
                conn.execute(
                    "UPDATE tracking_intervals SET ended_at = MAX(started_at, ?) "
                    "WHERE user_id = ? AND ended_at IS NULL",
                    (ended, user_text),
                )
                conn.execute(
                    "UPDATE tracked_users SET active = 0, updated_at = ? WHERE user_id = ?",
                    (now, user_text),
                )
                return True

            return self._transaction(conn, action)

        return await self._run(operation)

    async def tracked_users(self) -> list[dict[str, Any]]:
        """Return every tracked-list row, active or not, ordered by user ID."""

        def operation() -> list[dict[str, Any]]:
            conn = self._conn()
            self._settings(conn)
            rows = [
                {
                    "user_id": int(row["user_id"]),
                    "active": bool(row["active"]),
                    "tracking_since": float(row["tracking_since"]),
                    "added_by": None if row["added_by"] is None else int(row["added_by"]),
                    "updated_at": float(row["updated_at"]),
                }
                for row in conn.execute("SELECT * FROM tracked_users")
            ]
            return sorted(rows, key=lambda item: item["user_id"])

        return await self._run(operation)

    async def active_user_ids(self) -> frozenset[int]:
        """Return the IDs of everyone currently tracked."""

        def operation() -> frozenset[int]:
            conn = self._conn()
            self._settings(conn)
            return frozenset(
                int(row["user_id"])
                for row in conn.execute("SELECT user_id FROM tracked_users WHERE active = 1")
            )

        return await self._run(operation)

    # -- Collection ---------------------------------------------------------

    async def add_message(
        self, user_id: int, message_id: int, channel_id: int, created_at: float
    ) -> bool:
        """Insert one message event, returning False for filtered or duplicate events."""
        inserted, _ = await self._add_message(
            user_id, message_id, channel_id, created_at, ordinary=False
        )
        return inserted

    async def add_message_with_reaction(
        self, user_id: int, message_id: int, channel_id: int, created_at: float, *, ordinary: bool
    ) -> tuple[bool, bool]:
        """Insert a message and advance the reaction interval in one transaction."""
        return await self._add_message(
            user_id, message_id, channel_id, created_at, ordinary=ordinary
        )

    async def _add_message(
        self, user_id: int, message_id: int, channel_id: int, created_at: float, *, ordinary: bool
    ) -> tuple[bool, bool]:
        created_at = self._timestamp(created_at)
        user_text = str(int(user_id))
        message_text = str(int(message_id))
        channel_text = str(int(channel_id))
        day = local_day(created_at, self.timezone)

        def operation() -> tuple[bool, bool]:
            conn = self._conn()

            def action() -> tuple[bool, bool]:
                settings = self._settings(conn)
                if bool(settings["paused"]):
                    return False, False
                person_start = self._collection_start(conn, user_text)
                if person_start is None:
                    return False, False
                if created_at < max(
                    float(settings["tracking_since"]),
                    float(settings["resume_boundary"]),
                    person_start,
                ):
                    return False, False
                pruned_before = settings["pruned_before"]
                if pruned_before is not None and created_at < float(pruned_before):
                    return False, False
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO messages(message_id, user_id, channel_id, created_at, day) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (message_text, user_text, channel_text, created_at, day),
                )
                if cursor.rowcount == 0:
                    return False, False
                self._daily_delta(conn, user_text, day, messages=1)
                self._update_busiest_day(conn, user_text, day)
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
                segments = self._open_segments(conn)
                at = max(now, float(coverage["checkpoint"]))
                for segment in segments:
                    at = max(at, float(segment["checkpoint"]))
                for segment in segments:
                    user_text = str(segment["user_id"])
                    self._add_voice_time(
                        conn,
                        user_text,
                        int(segment["visit_id"]),
                        float(segment["checkpoint"]),
                        at,
                    )
                    conn.execute(
                        "UPDATE voice_segments SET checkpoint = ? WHERE segment_id = ?",
                        (at, int(segment["segment_id"])),
                    )
                    self._record_last_voice(conn, user_text, str(segment["channel_id"]), at)
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
        """Update every tracked roster in a channel at an observed join or leave.

        Each other person whose open segment and roster are in ``channel_id``
        is attributed time through ``now`` first; a roster that already
        matches is skipped, and the member's own segment is never touched.
        """
        now = self._timestamp(now)
        channel_text = str(int(channel_id))
        member_text = str(int(member_id))

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                settings = self._settings(conn)
                if bool(settings["paused"]) or not bool(settings["connected"]):
                    return
                for segment in self._open_segments(conn):
                    user_text = str(segment["user_id"])
                    if user_text == member_text or str(segment["channel_id"]) != channel_text:
                        continue
                    company = conn.execute(
                        "SELECT * FROM voice_company_current WHERE user_id = ?", (user_text,)
                    ).fetchone()
                    if company is None or str(company["channel_id"]) != channel_text:
                        continue
                    members = set(json.loads(company["member_ids"]))
                    if (member_text in members) == joined:
                        continue
                    at = max(now, float(segment["checkpoint"]))
                    self._add_voice_time(
                        conn, user_text, int(segment["visit_id"]),
                        float(segment["checkpoint"]), at,
                    )
                    conn.execute(
                        "UPDATE voice_segments SET checkpoint = ? WHERE segment_id = ?",
                        (at, int(segment["segment_id"])),
                    )
                    self._record_last_voice(conn, user_text, channel_text, at)
                    if joined:
                        members.add(member_text)
                    else:
                        members.discard(member_text)
                    conn.execute(
                        "UPDATE voice_company_current SET member_ids = ?, checkpoint = ? "
                        "WHERE user_id = ?",
                        (json.dumps(sorted(members)), at, user_text),
                    )
                    self._advance_checkpoint(conn, at)

            self._transaction(conn, action)

        await self._run(operation)

    async def voice_transition(
        self,
        user_id: int,
        channel_id: int | None,
        now: float,
        complete_start: bool = True,
        companions: set[int] | frozenset[int] = frozenset(),
    ) -> None:
        """Open, close, or move one tracked person's observed visit between channels."""
        now = self._timestamp(now)
        user_text = str(int(user_id))
        channel_text = None if channel_id is None else str(int(channel_id))

        def operation() -> None:
            conn = self._conn()

            def action() -> None:
                settings = self._settings(conn)
                if bool(settings["paused"]) or not bool(settings["connected"]):
                    return
                person_start = self._collection_start(conn, user_text)
                if person_start is None:
                    return
                segment = self._open_segment(conn, user_text)
                current_channel = None if segment is None else str(segment["channel_id"])
                if current_channel == channel_text:
                    return
                coverage = self._open_coverage(conn)
                # Nothing is observed before the person was on the tracked list.
                at = max(now, person_start)
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
                            "SELECT * FROM voice_visits WHERE user_id = ? "
                            "ORDER BY visit_id DESC LIMIT 1",
                            (user_text,),
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
                                   user_id, started_at, complete_start, complete_end, observed_seconds
                               ) VALUES (?, ?, ?, 0, 0)""",
                            (user_text, at, int(complete_start)),
                        )
                        visit_id = int(cursor.lastrowid)
                        if complete_start:
                            self._daily_delta(
                                conn,
                                user_text,
                                local_day(at, self.timezone),
                                voice_visits=1,
                            )
                    conn.execute(
                        """INSERT INTO voice_segments(
                               visit_id, user_id, channel_id, started_at, checkpoint
                           ) VALUES (?, ?, ?, ?, ?)""",
                        (visit_id, user_text, channel_text, at, at),
                    )
                    members = sorted({
                        str(int(member)) for member in companions
                        if int(member) > 0 and str(int(member)) != user_text
                    })
                    conn.execute(
                        """INSERT INTO voice_company_current(user_id, channel_id, member_ids, checkpoint)
                           VALUES (?, ?, ?, ?)""",
                        (user_text, channel_text, json.dumps(members), at),
                    )
                    self._record_last_voice(conn, user_text, channel_text, at)

                self._advance_checkpoint(conn, at)

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
                segments = self._open_segments(conn)
                if coverage is None and not segments and not bool(settings["connected"]):
                    return

                checkpoint = (
                    float(coverage["checkpoint"])
                    if coverage is not None
                    else float(settings["last_checkpoint"] or now)
                )
                for segment in segments:
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
                    checkpoint = min(checkpoint, voice_checkpoint)
                if segments:
                    conn.execute("DELETE FROM voice_company_current")
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

    # -- Reports ------------------------------------------------------------

    @staticmethod
    def _person_since(settings: sqlite3.Row, tracked: sqlite3.Row | None) -> float:
        """Return where a person's own tracking clock starts.

        It is never earlier than the database clock, so deleting all data
        restarts every person's history even if their row predates it.
        """
        since = float(settings["tracking_since"])
        return since if tracked is None else max(since, float(tracked["tracking_since"]))

    @staticmethod
    def _tracking_intervals(conn: sqlite3.Connection, user_text: str) -> list[tuple[float, float]]:
        """Return a person's tracked-list intervals; an open one runs without end."""
        return [
            (float(row["started_at"]), math.inf if row["ended_at"] is None else float(row["ended_at"]))
            for row in conn.execute(
                "SELECT started_at, ended_at FROM tracking_intervals WHERE user_id = ? "
                "ORDER BY started_at, interval_id",
                (user_text,),
            )
        ]

    def _gap_seconds(
        self, conn: sqlite3.Connection, user_text: str, since: float, start: float, end: float
    ) -> float:
        """Return a person's missing-coverage seconds in ``[start, end)``.

        That is the collector's recorded gaps while they were tracked, plus
        every moment after their own start that they were not on the tracked
        list, so an untracked stretch is missing coverage rather than quiet time.
        """
        low = max(start, since)
        if end <= low:
            return 0.0
        intervals = self._tracking_intervals(conn, user_text)
        gaps = [
            (
                max(low, float(row["started_at"])),
                end if row["ended_at"] is None else min(end, float(row["ended_at"])),
            )
            for row in conn.execute(
                "SELECT started_at, ended_at FROM coverage_gaps WHERE started_at < ? "
                "AND COALESCE(ended_at, ?) > ?",
                (end, end, low),
            )
        ]
        recorded = sum(b - a for a, b in intersect_intervals(gaps, intervals))
        tracked = sum(b - a for a, b in intersect_intervals([(low, end)], intervals))
        return recorded + (end - low - tracked)

    @staticmethod
    def _is_live(settings: sqlite3.Row, include_live: bool) -> bool:
        """True when open voice and coverage may be reported through now."""
        return include_live and bool(settings["connected"]) and not bool(settings["paused"])

    def _live_segment(
        self, conn: sqlite3.Connection, settings: sqlite3.Row, user_text: str, include_live: bool
    ) -> sqlite3.Row | None:
        """Return a person's open voice segment when live time may be reported."""
        if not self._is_live(settings, include_live):
            return None
        return self._open_segment(conn, user_text)

    @staticmethod
    def _detail_since(settings: sqlite3.Row, start: float, since: float) -> float:
        """Return where retained message and voice detail begins at or after ``start``.

        Deletion also moves ``pruned_before``, but only pruning past the
        person's tracking start means retained detail is missing.
        """
        pruned = settings["pruned_before"]
        if pruned is None or float(pruned) <= since:
            return start
        return max(start, float(pruned))

    def _watched_by_day(
        self,
        conn: sqlite3.Connection,
        user_text: str,
        start: float,
        end: float,
        live_until: float | None = None,
    ) -> dict[str, float]:
        """Return seconds a person was watched per local day inside ``[start, end)``.

        Watched time is collector coverage that fell inside the person's
        tracking intervals. An open coverage interval counts through its
        checkpoint, or through ``live_until`` when the collector is currently
        reliable.
        """
        pieces: list[tuple[float, float]] = []
        for row in conn.execute(
            "SELECT started_at, ended_at, checkpoint FROM coverage_intervals "
            "WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?) ORDER BY started_at",
            (end, start),
        ):
            if row["ended_at"] is not None:
                interval_end = float(row["ended_at"])
            elif live_until is not None:
                interval_end = max(float(row["checkpoint"]), live_until)
            else:
                interval_end = float(row["checkpoint"])
            pieces.append((max(start, float(row["started_at"])), min(end, interval_end)))
        watched: dict[str, float] = {}
        for piece_start, piece_end in intersect_intervals(
            pieces, self._tracking_intervals(conn, user_text)
        ):
            for day, seconds in split_interval_by_day(piece_start, piece_end, self.timezone):
                watched[day] = watched.get(day, 0.0) + seconds
        return watched

    def _window_totals(
        self,
        conn: sqlite3.Connection,
        settings: sqlite3.Row,
        user_text: str,
        start: float,
        end: float,
        now: float,
        include_live: bool,
    ) -> dict[str, Any]:
        """Return exact detail-based totals for one person in ``[start, end)``."""
        messages = int(conn.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id = ? AND created_at >= ? AND created_at < ?",
            (user_text, start, end),
        ).fetchone()[0])
        visits = int(conn.execute(
            "SELECT COUNT(*) FROM voice_visits WHERE user_id = ? AND complete_start = 1 "
            "AND started_at >= ? AND started_at < ?",
            (user_text, start, end),
        ).fetchone()[0])
        voice = 0.0
        for row in conn.execute(
            "SELECT started_at, COALESCE(ended_at, checkpoint) AS ended_at FROM voice_segments "
            "WHERE user_id = ? AND started_at < ? AND COALESCE(ended_at, checkpoint) > ?",
            (user_text, end, start),
        ):
            voice += interval_overlap(float(row["started_at"]), float(row["ended_at"]), start, end)
        segment = self._live_segment(conn, settings, user_text, include_live)
        if segment is not None:
            voice += interval_overlap(float(segment["checkpoint"]), max(now, float(segment["checkpoint"])), start, end)
        live_until = now if self._is_live(settings, include_live) else None
        watched = sum(self._watched_by_day(conn, user_text, start, end, live_until).values())
        return {
            "start": start,
            "end": end,
            "messages": messages,
            "voice_seconds": voice,
            "voice_visits": visits,
            "watched_seconds": watched,
        }

    async def stats(
        self, user_id: int, period: str, now: float, *, include_live: bool = True
    ) -> dict[str, Any]:
        """Return one person's message, voice, active-day, visit, and gap totals."""
        now = self._timestamp(now)
        user_text = str(int(user_id))

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            tracked = self._tracked_row(conn, user_text)
            since = self._person_since(settings, tracked)
            start, end = period_bounds(period, now, self.timezone, since)
            first_day = local_day(start, self.timezone)
            last_day = local_day(end, self.timezone)
            aggregate = conn.execute(
                """SELECT COALESCE(SUM(messages), 0) AS messages,
                          COALESCE(SUM(voice_seconds), 0) AS voice_seconds,
                          COALESCE(SUM(voice_visits), 0) AS voice_visits,
                          COALESCE(SUM(active), 0) AS active_days
                   FROM daily_stats WHERE user_id = ? AND day >= ? AND day <= ?""",
                (user_text, first_day, last_day),
            ).fetchone()

            voice_seconds = float(aggregate["voice_seconds"])
            active_dates = {
                str(row["day"])
                for row in conn.execute(
                    "SELECT day FROM daily_stats WHERE user_id = ? AND day >= ? AND day <= ? "
                    "AND active = 1",
                    (user_text, first_day, last_day),
                )
            }
            segment = self._live_segment(conn, settings, user_text, include_live)
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

            return {
                "messages": int(aggregate["messages"]),
                "voice_seconds": voice_seconds,
                "voice_visits": int(aggregate["voice_visits"]),
                "active_days": len(active_dates),
                "tracking_since": since,
                "gap_seconds": self._gap_seconds(conn, user_text, since, start, end),
                "paused": bool(settings["paused"]),
                "tracked": tracked is not None and bool(tracked["active"]),
                "known": tracked is not None,
            }

        return await self._run(operation)

    async def ranking(
        self, period: str, now: float, *, include_live: bool = True
    ) -> list[dict[str, Any]]:
        """Return one row per tracked person or person with activity in a period.

        Totals follow ``stats``: stored daily totals plus each person's live
        segment, with live days counted as active. Bounds use the database
        clock; rows are ordered by user ID and the caller sorts them.
        """
        now = self._timestamp(now)

        def operation() -> list[dict[str, Any]]:
            conn = self._conn()
            settings = self._settings(conn)
            start, end = period_bounds(
                period, now, self.timezone, float(settings["tracking_since"])
            )
            totals: dict[str, dict[str, Any]] = {}

            def entry(user_text: str) -> dict[str, Any]:
                return totals.setdefault(
                    user_text,
                    {"messages": 0, "voice_seconds": 0.0, "voice_visits": 0, "days": set()},
                )

            for row in conn.execute(
                "SELECT user_id, day, messages, voice_seconds, voice_visits, active "
                "FROM daily_stats WHERE day >= ? AND day <= ?",
                (local_day(start, self.timezone), local_day(end, self.timezone)),
            ):
                item = entry(str(row["user_id"]))
                item["messages"] += int(row["messages"])
                item["voice_seconds"] += float(row["voice_seconds"])
                item["voice_visits"] += int(row["voice_visits"])
                if bool(row["active"]):
                    item["days"].add(str(row["day"]))
            if self._is_live(settings, include_live):
                for segment in self._open_segments(conn):
                    live_start = float(segment["checkpoint"])
                    clipped_start = max(live_start, start)
                    clipped_end = min(max(now, live_start), end)
                    if clipped_end > clipped_start:
                        item = entry(str(segment["user_id"]))
                        item["voice_seconds"] += clipped_end - clipped_start
                        item["days"].update(
                            day
                            for day, seconds in split_interval_by_day(
                                clipped_start, clipped_end, self.timezone
                            )
                            if seconds > 0
                        )
            active = {
                str(row["user_id"]): bool(row["active"])
                for row in conn.execute("SELECT user_id, active FROM tracked_users")
            }
            rows: list[dict[str, Any]] = []
            for user_text in set(totals) | {user for user, on in active.items() if on}:
                item = totals.get(user_text) or entry(user_text)
                rows.append({
                    "user_id": int(user_text),
                    "messages": item["messages"],
                    "voice_seconds": item["voice_seconds"],
                    "voice_visits": item["voice_visits"],
                    "active_days": len(item["days"]),
                    "tracked": active.get(user_text, False),
                })
            return sorted(rows, key=lambda row: row["user_id"])

        return await self._run(operation)

    async def period_comparison(
        self, user_id: int, period: str, now: float, *, include_live: bool = True
    ) -> dict[str, Any]:
        """Compare a person's period so far with the previous period up to the same point.

        Both windows use retained detail so partial days compare fairly.
        ``previous`` is ``None`` with a ``reason`` of ``all``, ``untracked``, or
        ``pruned`` when no honest comparison exists.
        """
        now = self._timestamp(now)
        user_text = str(int(user_id))

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            since = self._person_since(settings, self._tracked_row(conn, user_text))
            start, end = period_bounds(period, now, self.timezone, since)
            current = self._window_totals(conn, settings, user_text, start, end, now, include_live)
            previous_bounds = previous_period_bounds(period, now, self.timezone)
            result: dict[str, Any] = {"current": current, "previous": None, "reason": None}
            if previous_bounds is None:
                result["reason"] = "all"
            elif previous_bounds[0] < since:
                result["reason"] = "untracked"
            elif self._detail_since(settings, previous_bounds[0], since) > previous_bounds[0]:
                result["reason"] = "pruned"
            else:
                result["previous"] = self._window_totals(
                    conn, settings, user_text, *previous_bounds, now, include_live
                )
            return result

        return await self._run(operation)

    async def company_daily(
        self, user_id: int, period: str, now: float, *, include_live: bool = True
    ) -> list[dict[str, Any]]:
        """Return a person's voice seconds by local day, channel, and peer (0 means alone).

        ``seconds`` splits each shared second evenly among the peers present;
        ``full_seconds`` credits every peer with the whole second.
        """
        now = self._timestamp(now)
        user_text = str(int(user_id))

        def operation() -> list[dict[str, Any]]:
            conn = self._conn()
            settings = self._settings(conn)
            since = self._person_since(settings, self._tracked_row(conn, user_text))
            start, end = period_bounds(period, now, self.timezone, since)
            totals: dict[tuple[str, int, int], tuple[float, float]] = {}
            for row in conn.execute(
                """SELECT day, channel_id, member_id, seconds, full_seconds FROM voice_company_daily
                   WHERE user_id = ? AND day >= ? AND day <= ?""",
                (user_text, local_day(start, self.timezone), local_day(end, self.timezone)),
            ):
                key = (str(row["day"]), int(row["channel_id"]), int(row["member_id"]))
                totals[key] = (float(row["seconds"]), float(row["full_seconds"]))
            segment = self._live_segment(conn, settings, user_text, include_live)
            company = conn.execute(
                "SELECT * FROM voice_company_current WHERE user_id = ?", (user_text,)
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
        self, user_id: int, period: str, now: float, *, include_live: bool = True
    ) -> list[dict[str, Any]]:
        """Return split and full voice seconds by channel and peer (0 means alone)."""
        totals: dict[tuple[int, int], tuple[float, float]] = {}
        for row in await self.company_daily(user_id, period, now, include_live=include_live):
            key = (row["channel_id"], row["member_id"])
            split, full = totals.get(key, (0.0, 0.0))
            totals[key] = (split + row["seconds"], full + row["full_seconds"])
        return [
            {"channel_id": channel_id, "member_id": member_id, "seconds": split, "full_seconds": full}
            for (channel_id, member_id), (split, full) in sorted(totals.items())
        ]

    async def daily_trend(
        self, user_id: int, period: str, now: float, *, include_live: bool = True
    ) -> list[dict[str, Any]]:
        """Return message, voice, and visit totals for every local day in a period.

        ``watched`` is true only for a finished day the collector covered in
        full while the person was tracked, so a day without activity can be
        called quiet rather than unknown.
        """
        now = self._timestamp(now)
        user_text = str(int(user_id))

        def operation() -> list[dict[str, Any]]:
            conn = self._conn()
            settings = self._settings(conn)
            since = self._person_since(settings, self._tracked_row(conn, user_text))
            start, end = period_bounds(period, now, self.timezone, since)
            # Days before tracking began are unknown, not quiet, so leave them out.
            start = max(start, since)
            first_day = local_date(start, self.timezone)
            last_day = local_date(end, self.timezone)
            watched = self._watched_by_day(
                conn, user_text, local_midnight(first_day, self.timezone), end
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
                   WHERE user_id = ? AND day >= ? AND day <= ?""",
                (user_text, first_day.isoformat(), last_day.isoformat()),
            ):
                entry = series.get(str(row["day"]))
                if entry is not None:
                    entry["messages"] = int(row["messages"])
                    entry["voice_seconds"] = float(row["voice_seconds"])
                    entry["voice_visits"] = int(row["voice_visits"])
            segment = self._live_segment(conn, settings, user_text, include_live)
            if segment is not None:
                live_start = max(float(segment["checkpoint"]), start)
                for piece_day, seconds in split_interval_by_day(live_start, end, self.timezone):
                    if piece_day in series:
                        series[piece_day]["voice_seconds"] += seconds
            return list(series.values())

        return await self._run(operation)

    async def message_times(self, user_id: int, period: str, now: float) -> dict[str, Any]:
        """Return a person's retained message send times in a period, oldest first.

        ``since`` is where retained message detail begins inside the period;
        older messages survive only in daily totals and have no send time.
        """
        now = self._timestamp(now)
        user_text = str(int(user_id))

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            since = self._person_since(settings, self._tracked_row(conn, user_text))
            start, end = period_bounds(period, now, self.timezone, since)
            times = [
                float(row[0])
                for row in conn.execute(
                    "SELECT created_at FROM messages WHERE user_id = ? "
                    "AND created_at >= ? AND created_at <= ? ORDER BY created_at",
                    (user_text, start, end),
                )
            ]
            return {
                "times": times,
                "since": self._detail_since(settings, start, since),
                "period_start": start,
            }

        return await self._run(operation)

    async def voice_hours(
        self, user_id: int, period: str, now: float, *, include_live: bool = True
    ) -> dict[str, Any]:
        """Return a person's retained observed voice seconds by local hour in a period."""
        now = self._timestamp(now)
        user_text = str(int(user_id))

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            person_since = self._person_since(settings, self._tracked_row(conn, user_text))
            start, end = period_bounds(period, now, self.timezone, person_since)
            spans = [
                (float(row["started_at"]), float(row["ended_at"]))
                for row in conn.execute(
                    "SELECT started_at, COALESCE(ended_at, checkpoint) AS ended_at "
                    "FROM voice_segments WHERE user_id = ? AND started_at < ? "
                    "AND COALESCE(ended_at, checkpoint) > ?",
                    (user_text, end, start),
                )
            ]
            segment = self._live_segment(conn, settings, user_text, include_live)
            if segment is not None:
                spans.append((float(segment["checkpoint"]), max(now, float(segment["checkpoint"]))))
            # Clip to retained detail so voice covers the same window as message times.
            since = self._detail_since(settings, start, person_since)
            hours = [0.0] * 24
            for span_start, span_end in spans:
                for hour, seconds in split_interval_by_hour(
                    max(since, span_start), min(end, span_end), self.timezone
                ):
                    hours[hour] += seconds
            return {"hours": hours, "since": since, "period_start": start}

        return await self._run(operation)

    async def records(
        self, user_id: int, now: float, *, include_live: bool = True
    ) -> dict[str, Any]:
        """Return a person's busiest message day, longest fully observed voice visit,
        and the observed length so far of a live visit."""
        now = self._timestamp(now)
        user_text = str(int(user_id))

        def operation() -> dict[str, Any]:
            conn = self._conn()
            settings = self._settings(conn)
            current_seconds: float | None = None
            current_complete_start = False
            segment = self._live_segment(conn, settings, user_text, include_live)
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
                "SELECT value, at FROM records WHERE user_id = ? AND record_type = 'busiest_day'",
                (user_text,),
            ).fetchone()
            longest = conn.execute(
                "SELECT value, at FROM records WHERE user_id = ? AND record_type = 'longest_visit'",
                (user_text,),
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

    async def last_voice(
        self, user_id: int, now: float, *, include_live: bool = True
    ) -> dict[str, Any] | None:
        """Return a person's latest observed voice channel and time, or no observation."""
        now = self._timestamp(now)
        user_text = str(int(user_id))

        def operation() -> dict[str, Any] | None:
            conn = self._conn()
            settings = self._settings(conn)
            segment = self._live_segment(conn, settings, user_text, include_live)
            if segment is not None:
                return {
                    "channel_id": int(segment["channel_id"]),
                    "seen_at": max(now, float(segment["checkpoint"])),
                    "current": True,
                    "observed_since": float(segment["started_at"]),
                }
            row = conn.execute(
                "SELECT channel_id, seen_at FROM last_voice WHERE user_id = ?", (user_text,)
            ).fetchone()
            if row is None:
                return None
            return {
                "channel_id": int(row["channel_id"]),
                "seen_at": float(row["seen_at"]),
                "current": False,
                "observed_since": None,
            }

        return await self._run(operation)

    # -- Deletion -----------------------------------------------------------

    async def delete_data(self, actor_id: int, now: float) -> None:
        """Erase everyone's statistics and all managed backups, then pause collection.

        The tracked list survives as an operational setting: active people
        restart with a fresh tracking interval at ``now``, inactive people
        (who only had history) are dropped.
        """
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
                conn.execute("DELETE FROM tracking_intervals")
                conn.execute("DELETE FROM tracked_users WHERE active = 0")
                conn.execute(
                    "UPDATE tracked_users SET tracking_since = ?, updated_at = ?", (now, now)
                )
                conn.execute(
                    "INSERT INTO tracking_intervals(user_id, started_at) "
                    "SELECT user_id, ? FROM tracked_users",
                    (now,),
                )
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

    async def delete_user_data(
        self,
        user_id: int,
        actor_id: int,
        now: float,
        *,
        reset_legacy_modes: bool = False,
    ) -> bool:
        """Erase one person's statistics and tracked-list row, leaving others untouched.

        Managed backups are removed first because they hold this person's data.
        Rows where the person appears only as a companion in someone else's
        company belong to that other person and stay. Global collection is not
        paused. ``reset_legacy_modes`` also switches the Leland-only evil and
        reaction modes off. Returns False when nothing existed for the person.
        """
        self._timestamp(now)
        int(actor_id)  # Validated for symmetry; deletion is not attributed in storage.
        user_text = str(int(user_id))

        def operation() -> bool:
            conn = self._conn()
            self._settings(conn)
            existed = any(
                conn.execute(f"SELECT 1 FROM {table} WHERE user_id = ? LIMIT 1", (user_text,)).fetchone()
                is not None
                for table in (
                    "tracked_users", "tracking_intervals", "messages", "daily_stats",
                    "voice_visits", "voice_company_current", "voice_company_daily",
                    "last_voice", "records",
                )
            )
            if existed:
                # As with global deletion: erase backups before the live rows, and
                # keep the database intact if a managed backup cannot be removed.
                self._remove_managed_backups()

            def action() -> None:
                conn.execute("DELETE FROM voice_segments WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM voice_visits WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM messages WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM daily_stats WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM voice_company_current WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM voice_company_daily WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM last_voice WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM records WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM tracking_intervals WHERE user_id = ?", (user_text,))
                conn.execute("DELETE FROM tracked_users WHERE user_id = ?", (user_text,))
                if reset_legacy_modes:
                    conn.execute(
                        "UPDATE settings SET evil_mode = 0, reaction_mode = 0, "
                        "reaction_countdown = NULL WHERE singleton = 1"
                    )

            if existed or reset_legacy_modes:
                self._transaction(conn, action)
            if existed:
                try:
                    self._compact_after_deletion(conn)
                except sqlite3.Error:
                    logger.exception("Data deletion committed, but database compaction failed")
            return existed

        return await self._run(operation)

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
