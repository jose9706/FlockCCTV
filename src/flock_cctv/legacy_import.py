"""One-time import of a single-target Leland Tracker database into Flock CCTV.

The old bot stored one person's statistics in a schema-version-9 SQLite file.
Flock keys every per-person table by ``user_id``, so this tool snapshots the old
file, creates a fresh Flock database through :class:`Store`, and copies the old
rows over in one transaction with ``user_id`` set to the Leland user. The source
is only ever opened read-only and is never modified. Output reports row counts
only: never message, channel, or user IDs and never any content.

Run it once, with the bot stopped::

    python -m flock_cctv.legacy_import --source OLD.sqlite3 --database NEW.sqlite3 \
        --backups DIR --guild-id G --leland-user-id L --timezone TZ [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .stats import get_timezone
from .storage import Store, StoreError

LEGACY_SCHEMA_VERSION = 9
DEFAULT_DATABASE_PATH = "data/tracker.sqlite3"
DEFAULT_BACKUP_DIR = "data/backups"
DEFAULT_TIMEZONE = "America/Costa_Rica"

# Tables copied to the new database, in dependency order (visits before segments).
_COPIED_TABLES = (
    "messages",
    "daily_stats",
    "voice_visits",
    "voice_segments",
    "voice_company_current",
    "voice_company_daily",
    "last_voice",
    "records",
    "coverage_intervals",
    "coverage_gaps",
    "admin_overrides",
)
# Tables whose row count must be identical after the normal startup recovery.
# Recovery closes open coverage/segments in place and clears the live company
# roster, and it may add a restart gap or recompute a longest-visit record.
_STABLE_TABLES = (
    "messages",
    "daily_stats",
    "voice_visits",
    "voice_segments",
    "voice_company_daily",
    "last_voice",
    "coverage_intervals",
    "admin_overrides",
)
# Old settings columns carried over unchanged; guild/timezone identity is kept
# from the (validated) source and tracking_since also seeds Store.initialize.
_SETTINGS_FIELDS = (
    "tracking_since",
    "paused",
    "paused_by",
    "connected",
    "last_checkpoint",
    "resume_boundary",
    "pruned_before",
    "retention_days",
    "evil_mode",
    "reaction_mode",
    "reaction_countdown",
)
_AUTOINCREMENT_TABLES = {
    "voice_visits": "visit_id",
    "voice_segments": "segment_id",
    "coverage_intervals": "interval_id",
    "coverage_gaps": "gap_id",
}
_SIDECARS = ("-wal", "-shm", "-journal")


class LegacyImportError(RuntimeError):
    """A validation or copy failure with a message that is safe to print."""


def _sidecars(path: Path) -> list[Path]:
    return [path.with_name(path.name + suffix) for suffix in _SIDECARS]


def _snapshot_source(source: Path) -> sqlite3.Connection:
    """Copy the source into memory with the backup API, opening it read-only."""
    if not source.is_file():
        raise LegacyImportError("source database does not exist or is not a file")
    snapshot = sqlite3.connect(":memory:")
    try:
        # mode=ro never writes the main file; the backup API sees WAL content.
        origin = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
        try:
            origin.backup(snapshot)
        finally:
            origin.close()
    except sqlite3.Error as exc:
        snapshot.close()
        raise LegacyImportError(f"could not read the source database: {exc}") from exc
    snapshot.row_factory = sqlite3.Row
    return snapshot


def _validate_source(
    snapshot: sqlite3.Connection, guild_id: int, leland_user_id: int, timezone: str
) -> sqlite3.Row:
    """Check version and identity; return the source settings row."""
    try:
        version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
        if version != LEGACY_SCHEMA_VERSION:
            raise LegacyImportError(
                f"source schema version is {version}; only version "
                f"{LEGACY_SCHEMA_VERSION} Leland Tracker databases can be imported"
            )
        tables = {
            row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing = sorted(({"settings"} | set(_COPIED_TABLES)) - tables)
        if missing:
            raise LegacyImportError(f"source database is missing tables: {', '.join(missing)}")
        settings_columns = {row[1] for row in snapshot.execute("PRAGMA table_info(settings)")}
        required = {"guild_id", "target_user_id", "timezone", *_SETTINGS_FIELDS}
        if not required <= settings_columns:
            raise LegacyImportError(
                "source settings table is not a Leland Tracker schema-9 settings table"
            )
        row = snapshot.execute("SELECT * FROM settings WHERE singleton = 1").fetchone()
    except sqlite3.Error as exc:
        raise LegacyImportError(f"source database is not a valid Leland database: {exc}") from exc
    if row is None:
        raise LegacyImportError("source database has no settings row")
    if str(row["guild_id"]) != str(guild_id):
        raise LegacyImportError("source database belongs to a different guild than --guild-id")
    if str(row["target_user_id"]) != str(leland_user_id):
        raise LegacyImportError(
            "source database tracked a different user than --leland-user-id"
        )
    if row["timezone"] != timezone:
        raise LegacyImportError("source database timezone differs from --timezone")
    try:
        tracking_since = float(row["tracking_since"])
    except (TypeError, ValueError):
        tracking_since = math.nan
    if not math.isfinite(tracking_since):
        raise LegacyImportError("source database has an invalid tracking_since")
    return row


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _source_counts(snapshot: sqlite3.Connection) -> dict[str, int]:
    counts = {table: _count(snapshot, table) for table in _COPIED_TABLES}
    counts["tracked_users"] = 1
    counts["tracking_intervals"] = 1
    counts["recovered_open_segments"] = int(
        snapshot.execute(
            "SELECT COUNT(*) FROM voice_segments WHERE ended_at IS NULL"
        ).fetchone()[0]
    )
    return counts


def _check_destination(database: Path) -> bool:
    """Refuse a live destination; return whether the file already existed."""
    if database.is_dir():
        raise LegacyImportError("--database must name a file, not a directory")
    existed = database.exists()
    if not existed:
        stale = [path for path in _sidecars(database) if path.exists()]
        if stale:
            raise LegacyImportError(
                "destination has leftover SQLite sidecar files without a database; "
                "remove them first"
            )
        return False
    try:
        conn = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
    except sqlite3.Error as exc:
        raise LegacyImportError(f"could not open the existing destination: {exc}") from exc
    try:
        has_settings = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        if has_settings and conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone():
            raise LegacyImportError(
                "destination database already has settings; the import never merges "
                "into an existing database"
            )
    except sqlite3.Error as exc:
        raise LegacyImportError(f"destination is not a usable SQLite database: {exc}") from exc
    finally:
        conn.close()
    return True


def _copy_all(
    snapshot: sqlite3.Connection,
    destination: sqlite3.Connection,
    settings: sqlite3.Row,
    leland_text: str,
) -> None:
    """Copy every row inside the caller's open transaction."""
    since = float(settings["tracking_since"])

    def rows(query: str) -> sqlite3.Cursor:
        return snapshot.execute(query)

    def insert(sql: str, query: str, transform: Callable[[sqlite3.Row], Sequence] | None = None) -> None:
        cursor = rows(query)
        while True:
            batch = cursor.fetchmany(5000)
            if not batch:
                return
            destination.executemany(
                sql, [tuple(row) if transform is None else tuple(transform(row)) for row in batch]
            )

    assignments = ", ".join(f"{field} = ?" for field in _SETTINGS_FIELDS)
    updated = destination.execute(
        f"UPDATE settings SET {assignments} WHERE singleton = 1",
        tuple(settings[field] for field in _SETTINGS_FIELDS),
    )
    if updated.rowcount != 1:
        raise LegacyImportError("destination settings row is missing")

    destination.execute(
        "INSERT INTO tracked_users(user_id, tracking_since, active, added_by, updated_at) "
        "VALUES (?, ?, 1, NULL, ?)",
        (leland_text, since, since),
    )
    destination.execute(
        "INSERT INTO tracking_intervals(user_id, started_at, ended_at) VALUES (?, ?, NULL)",
        (leland_text, since),
    )

    insert(
        "INSERT INTO messages(message_id, user_id, channel_id, created_at, day) "
        "VALUES (?, ?, ?, ?, ?)",
        "SELECT message_id, channel_id, created_at, day FROM messages",
        lambda r: (r[0], leland_text, r[1], r[2], r[3]),
    )
    insert(
        "INSERT INTO daily_stats(user_id, day, messages, voice_seconds, voice_visits, active) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        "SELECT day, messages, voice_seconds, voice_visits, active FROM daily_stats",
        lambda r: (leland_text, *r),
    )
    insert(
        "INSERT INTO voice_visits(visit_id, user_id, started_at, ended_at, complete_start, "
        "complete_end, observed_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
        "SELECT visit_id, started_at, ended_at, complete_start, complete_end, observed_seconds "
        "FROM voice_visits",
        lambda r: (r[0], leland_text, *r[1:]),
    )
    insert(
        "INSERT INTO voice_segments(segment_id, visit_id, user_id, channel_id, started_at, "
        "ended_at, checkpoint) VALUES (?, ?, ?, ?, ?, ?, ?)",
        "SELECT segment_id, visit_id, channel_id, started_at, ended_at, checkpoint "
        "FROM voice_segments",
        lambda r: (r[0], r[1], leland_text, *r[2:]),
    )
    insert(
        "INSERT INTO voice_company_current(user_id, channel_id, member_ids, checkpoint) "
        "VALUES (?, ?, ?, ?)",
        "SELECT channel_id, member_ids, checkpoint FROM voice_company_current",
        lambda r: (leland_text, *r),
    )
    insert(
        "INSERT INTO voice_company_daily(user_id, day, channel_id, member_id, seconds, "
        "full_seconds) VALUES (?, ?, ?, ?, ?, ?)",
        "SELECT day, channel_id, member_id, seconds, full_seconds FROM voice_company_daily",
        lambda r: (leland_text, *r),
    )
    insert(
        "INSERT INTO last_voice(user_id, channel_id, seen_at) VALUES (?, ?, ?)",
        "SELECT channel_id, seen_at FROM last_voice",
        lambda r: (leland_text, *r),
    )
    insert(
        "INSERT INTO records(user_id, record_type, value, at) VALUES (?, ?, ?, ?)",
        "SELECT record_type, value, at FROM records",
        lambda r: (leland_text, *r),
    )
    insert(
        "INSERT INTO coverage_intervals(interval_id, started_at, ended_at, checkpoint, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        "SELECT interval_id, started_at, ended_at, checkpoint, reason FROM coverage_intervals",
    )
    insert(
        "INSERT INTO coverage_gaps(gap_id, started_at, ended_at, reason) VALUES (?, ?, ?, ?)",
        "SELECT gap_id, started_at, ended_at, reason FROM coverage_gaps",
    )
    insert(
        "INSERT INTO admin_overrides(user_id, enabled) VALUES (?, ?)",
        "SELECT user_id, enabled FROM admin_overrides",
    )

    _advance_sequences(snapshot, destination)


def _advance_sequences(snapshot: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    """Make AUTOINCREMENT counters at least the source counters and every copied ID.

    Explicit-ID inserts already raise the counter to the maximum copied ID; the
    source counter can be higher when its newest rows were pruned, and IDs of
    pruned rows must not be reused.
    """
    has_sequence = snapshot.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'"
    ).fetchone()
    for table, column in _AUTOINCREMENT_TABLES.items():
        source_seq = 0
        if has_sequence:
            row = snapshot.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
            ).fetchone()
            source_seq = int(row[0]) if row else 0
        copied_max = int(
            destination.execute(f"SELECT COALESCE(MAX({column}), 0) FROM {table}").fetchone()[0]
        )
        wanted = max(source_seq, copied_max)
        if wanted <= 0:
            continue
        current = destination.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
        ).fetchone()
        if current is None:
            destination.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, wanted)
            )
        elif int(current[0]) < wanted:
            destination.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (wanted, table)
            )


def _copy_and_verify(
    snapshot: sqlite3.Connection,
    database: Path,
    settings: sqlite3.Row,
    leland_text: str,
    expected: Mapping[str, int],
) -> None:
    """Copy everything in one transaction and verify counts before committing."""
    destination = sqlite3.connect(str(database), timeout=30.0, isolation_level=None)
    try:
        destination.execute("PRAGMA foreign_keys = ON")
        destination.execute("PRAGMA busy_timeout = 30000")
        destination.execute("BEGIN IMMEDIATE")
        try:
            existing = _count(destination, "settings")
            if existing != 1:
                raise LegacyImportError("destination schema was not initialized as expected")
            for table in (*_COPIED_TABLES, "tracked_users", "tracking_intervals"):
                if _count(destination, table):
                    raise LegacyImportError("destination already contains data")
            _copy_all(snapshot, destination, settings, leland_text)
            for table in (*_COPIED_TABLES, "tracked_users", "tracking_intervals"):
                if _count(destination, table) != expected[table]:
                    raise LegacyImportError(f"row count mismatch after copying {table}")
            if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise LegacyImportError("copied rows violate foreign key constraints")
            destination.execute("COMMIT")
        except BaseException:
            if destination.in_transaction:
                destination.execute("ROLLBACK")
            raise
    except sqlite3.Error as exc:
        raise LegacyImportError(f"copy failed and was rolled back: {exc}") from exc
    finally:
        destination.close()


def _verify_after_recovery(database: Path, expected: Mapping[str, int]) -> None:
    conn = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
    try:
        for table in _STABLE_TABLES:
            if _count(conn, table) != expected[table]:
                raise LegacyImportError(f"row count mismatch for {table} after recovery")
        for table in ("tracked_users", "tracking_intervals"):
            if _count(conn, table) != expected[table]:
                raise LegacyImportError(f"row count mismatch for {table} after recovery")
        if conn.execute("SELECT 1 FROM voice_segments WHERE ended_at IS NULL").fetchone():
            raise LegacyImportError("an open voice segment remained after recovery")
        if conn.execute("SELECT 1 FROM coverage_intervals WHERE ended_at IS NULL").fetchone():
            raise LegacyImportError("open coverage remained after recovery")
        if _count(conn, "voice_company_current"):
            raise LegacyImportError("a live company roster remained after recovery")
    except sqlite3.Error as exc:
        raise LegacyImportError(f"post-import verification failed: {exc}") from exc
    finally:
        conn.close()


def _remove_new_destination(database: Path) -> None:
    for path in (database, *_sidecars(database)):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


async def import_legacy(
    source: Path,
    database: Path,
    backups: Path,
    guild_id: int,
    leland_user_id: int,
    timezone: str,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Import a schema-9 Leland database; return row counts (never any content).

    The returned mapping has one count per copied table plus ``tracked_users``,
    ``tracking_intervals`` and ``recovered_open_segments`` (source segments that
    startup recovery closes as incomplete). Raises :class:`LegacyImportError`
    for every validation or copy failure.
    """
    source = Path(source)
    database = Path(database)
    backups = Path(backups)
    guild_id = int(guild_id)
    leland_user_id = int(leland_user_id)
    if guild_id <= 0 or leland_user_id <= 0:
        raise LegacyImportError("guild ID and Leland user ID must be positive")
    if not timezone:
        raise LegacyImportError("timezone must not be empty")
    try:
        get_timezone(timezone)
    except Exception as exc:
        raise LegacyImportError("timezone is not a valid IANA timezone") from exc
    try:
        same_file = source.exists() and database.exists() and os.path.samefile(source, database)
    except OSError:
        same_file = False
    if same_file or source.resolve() == database.resolve():
        raise LegacyImportError("--database must be a different file from --source")
    destination_resolved, backups_resolved = database.resolve(), backups.resolve()
    if (
        destination_resolved == backups_resolved
        or destination_resolved.is_relative_to(backups_resolved)
        or backups_resolved.is_relative_to(destination_resolved)
    ):
        raise LegacyImportError("--database and --backups must be separate locations")

    snapshot = _snapshot_source(source)
    try:
        settings = _validate_source(snapshot, guild_id, leland_user_id, timezone)
        counts = _source_counts(snapshot)
        existed = _check_destination(database)
        if dry_run:
            return counts

        leland_text = str(leland_user_id)
        tracking_since = float(settings["tracking_since"])
        database.parent.mkdir(parents=True, exist_ok=True)
        try:
            store = Store(database, backups, timezone)
            try:
                await store.initialize(tracking_since, guild_id)
            finally:
                await store.close()
            _copy_and_verify(snapshot, database, settings, leland_text, counts)
            # Normal startup recovery closes anything the source left open
            # (segments at their checkpoints, coverage, live company roster).
            store = Store(database, backups, timezone)
            try:
                await store.initialize(time.time(), guild_id)
            finally:
                await store.close()
            _verify_after_recovery(database, counts)
        except BaseException:
            if not existed:
                _remove_new_destination(database)
            raise
        return counts
    except StoreError as exc:
        raise LegacyImportError(f"destination storage error: {exc}") from exc
    finally:
        snapshot.close()


def format_summary(counts: Mapping[str, int], *, dry_run: bool) -> str:
    """Render counts only; no IDs and no data from the database."""
    lines = ["Dry run: nothing was written." if dry_run else "Import complete."]
    lines.append("Rows " + ("that would be imported" if dry_run else "imported") + ":")
    for name, value in counts.items():
        if name == "recovered_open_segments":
            continue
        lines.append(f"  {name}: {value}")
    lines.append(
        f"Open voice segments "
        f"{'that recovery would close' if dry_run else 'closed by recovery'} as incomplete: "
        f"{counts.get('recovered_open_segments', 0)}"
    )
    return "\n".join(lines)


def _build_parser(env: Mapping[str, str]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m flock_cctv.legacy_import",
        description=(
            "One-time import of a Leland Tracker (schema 9) database into a new "
            "Flock CCTV database. The source file is never modified."
        ),
        epilog=(
            "Options other than --source fall back to the DATABASE_PATH, BACKUP_DIR, "
            "GUILD_ID, LELAND_USER_ID and TIMEZONE environment variables."
        ),
    )
    parser.add_argument("--source", required=True, type=Path, help="old Leland SQLite database")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(env.get("DATABASE_PATH") or DEFAULT_DATABASE_PATH),
        help="new Flock database to create (default: $DATABASE_PATH)",
    )
    parser.add_argument(
        "--backups",
        type=Path,
        default=Path(env.get("BACKUP_DIR") or DEFAULT_BACKUP_DIR),
        help="Flock backup directory (default: $BACKUP_DIR)",
    )
    parser.add_argument("--guild-id", default=env.get("GUILD_ID"), help="default: $GUILD_ID")
    parser.add_argument(
        "--leland-user-id", default=env.get("LELAND_USER_ID"), help="default: $LELAND_USER_ID"
    )
    parser.add_argument(
        "--timezone",
        default=env.get("TIMEZONE") or DEFAULT_TIMEZONE,
        help="IANA timezone, must match the source (default: $TIMEZONE)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the source and print counts without writing anything",
    )
    return parser


def _positive_int(parser: argparse.ArgumentParser, value: object, name: str) -> int:
    try:
        result = int(str(value).strip())
    except (TypeError, ValueError):
        result = 0
    if result <= 0:
        parser.error(f"{name} is required and must be a positive integer")
    return result


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """CLI entry point; returns 0 on success and non-zero on any failure."""
    parser = _build_parser(os.environ if env is None else env)
    args = parser.parse_args(argv)  # exits 2 on a usage error
    guild_id = _positive_int(parser, args.guild_id, "--guild-id (or GUILD_ID)")
    leland_id = _positive_int(parser, args.leland_user_id, "--leland-user-id (or LELAND_USER_ID)")
    try:
        counts = asyncio.run(
            import_legacy(
                args.source.expanduser(),
                args.database.expanduser(),
                args.backups.expanduser(),
                guild_id,
                leland_id,
                str(args.timezone).strip(),
                dry_run=args.dry_run,
            )
        )
    except LegacyImportError as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        print(f"Import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(format_summary(counts, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
