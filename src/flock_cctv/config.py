"""Validated runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _positive_id(value: str | None, name: str, *, optional: bool = False) -> int | None:
    if value is None or not value.strip():
        if optional:
            return None
        raise ValueError(f"{name} is required")
    raw = value.strip()
    if not raw.isdecimal():
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(raw)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _channel_ids(value: str | None, name: str) -> frozenset[int] | None:
    """Parse an allowlist, with ``*`` representing all visible channels."""
    if value is None or value.strip() == "*":
        return None
    if not value.strip():
        raise ValueError(f"{name} must be '*' or a comma-separated list of IDs")
    items = [part.strip() for part in value.split(",")]
    if any(not part for part in items):
        raise ValueError(f"{name} must be '*' or a comma-separated list of IDs")
    parsed = [_positive_id(part, name) for part in items]
    return frozenset(parsed)  # type: ignore[arg-type]


def _positive_int(value: str | None, name: str, default: int) -> int:
    if value is None or not value.strip():
        return default
    raw = value.strip()
    if not raw.isdecimal() or int(raw) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(raw)


def _user_ids(value: str | None, name: str) -> frozenset[int]:
    if value is None or not value.strip():
        return frozenset()
    parts = [part.strip() for part in value.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{name} must be a comma-separated list of user IDs")
    return frozenset(int(_positive_id(part, name)) for part in parts)


def _public_channel_ids(value: str | None) -> frozenset[int] | None:
    """Blank keeps reports private; '*' makes them public in every channel."""
    if value is None or not value.strip():
        return frozenset()
    return _channel_ids(value, "PUBLIC_REPORT_CHANNEL_IDS")


@dataclass(frozen=True, slots=True)
class Config:
    token: str = field(repr=False)
    guild_id: int
    target_user_id: int
    owner_user_id: int
    output_channel_id: int | None
    text_channel_ids: frozenset[int] | None
    voice_channel_ids: frozenset[int] | None
    timezone: str
    database_path: Path
    backup_dir: Path
    admin_user_ids: frozenset[int] = frozenset()
    public_report_channel_ids: frozenset[int] | None = frozenset()
    checkpoint_seconds: int = 60
    retention_days: int = 90

    @classmethod
    def from_env(cls) -> "Config":
        """Load and validate configuration from the process environment."""
        env = os.environ
        token = env.get("DISCORD_TOKEN", "").strip()
        if not token or any(char.isspace() for char in token):
            raise ValueError("DISCORD_TOKEN is required and must not contain whitespace")

        guild_id = _positive_id(env.get("GUILD_ID"), "GUILD_ID")
        target_user_id = _positive_id(env.get("TARGET_USER_ID"), "TARGET_USER_ID")
        owner_user_id = _positive_id(env.get("OWNER_USER_ID"), "OWNER_USER_ID")
        admin_user_ids = _user_ids(env.get("ADMIN_USER_IDS"), "ADMIN_USER_IDS")
        if target_user_id == owner_user_id or target_user_id in admin_user_ids:
            raise ValueError("TARGET_USER_ID cannot be a tracker admin")
        output_channel_id = _positive_id(
            env.get("OUTPUT_CHANNEL_ID"), "OUTPUT_CHANNEL_ID", optional=True
        )
        timezone = env.get("TIMEZONE", "America/Costa_Rica").strip()
        if not timezone:
            raise ValueError("TIMEZONE must not be empty")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"TIMEZONE is not a valid IANA timezone: {timezone!r}") from exc

        database_path = Path(env.get("DATABASE_PATH", "data/tracker.sqlite3")).expanduser()
        backup_dir = Path(env.get("BACKUP_DIR", "data/backups")).expanduser()
        database_path = database_path.resolve()
        backup_dir = backup_dir.resolve()
        if database_path == backup_dir:
            raise ValueError("DATABASE_PATH and BACKUP_DIR must be different locations")
        if database_path.is_relative_to(backup_dir) or backup_dir.is_relative_to(database_path):
            raise ValueError("DATABASE_PATH and BACKUP_DIR must not contain one another")
        if database_path.exists() and database_path.is_dir():
            raise ValueError("DATABASE_PATH must name a file, not a directory")
        if backup_dir.exists() and not backup_dir.is_dir():
            raise ValueError("BACKUP_DIR must name a directory")

        return cls(
            token=token,
            guild_id=int(guild_id),
            target_user_id=int(target_user_id),
            owner_user_id=int(owner_user_id),
            output_channel_id=output_channel_id,
            text_channel_ids=_channel_ids(env.get("TEXT_CHANNEL_IDS", "*"), "TEXT_CHANNEL_IDS"),
            voice_channel_ids=_channel_ids(env.get("VOICE_CHANNEL_IDS", "*"), "VOICE_CHANNEL_IDS"),
            timezone=timezone,
            database_path=database_path,
            backup_dir=backup_dir,
            admin_user_ids=admin_user_ids,
            public_report_channel_ids=_public_channel_ids(env.get("PUBLIC_REPORT_CHANNEL_IDS")),
            checkpoint_seconds=_positive_int(
                env.get("CHECKPOINT_SECONDS"), "CHECKPOINT_SECONDS", 60
            ),
            retention_days=_positive_int(env.get("RETENTION_DAYS"), "RETENTION_DAYS", 90),
        )
