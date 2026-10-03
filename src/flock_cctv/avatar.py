"""The bot's fixed Discord profile: name, description, and bundled avatar."""

from __future__ import annotations

import hashlib
from importlib import resources

BOT_USERNAME = "Flock CCTV"
BOT_DESCRIPTION = (
    "Flock CCTV keeps an eye on the server. It tracks an admin-managed list of "
    "people, counting their messages and their time in voice, and turns that "
    "into reports, trends and leaderboards. It never stores what anyone writes."
)


def profile_avatar() -> bytes:
    """Return the bundled Flock camera avatar image."""
    return resources.files(__package__).joinpath("assets/avatar.jpg").read_bytes()


def avatar_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
