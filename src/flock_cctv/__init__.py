"""Leland Tracker: message and voice statistics for one shared Discord server."""

from __future__ import annotations

import functools
import re
import subprocess
from pathlib import Path

# Single source of truth for the release number (pyproject.toml reads it).
# Bump it in the pull request that changes behavior: MAJOR for incompatible
# storage or configuration changes, MINOR for features, PATCH for fixes.
__version__ = "0.5.0"

_REVISION_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _build_revision() -> str | None:
    """Commit stamped into the package by the updater's staging step."""
    try:
        from . import _build  # generated file; absent outside updater installs
    except ImportError:
        return None
    revision = getattr(_build, "REVISION", None)
    return revision if isinstance(revision, str) and _REVISION_RE.fullmatch(revision) else None


@functools.cache
def _checkout_revision() -> str | None:
    """Commit of a development checkout the package is imported from.

    Cached: the bot asks for its version from async handlers, and the git call blocks.
    """
    source = Path(__file__).resolve().parent
    if not (source.parent.parent / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"), cwd=source, capture_output=True,
            text=True, timeout=5, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if _REVISION_RE.fullmatch(revision) else None


def revision() -> str | None:
    """Full commit ID of the running code, or None when it cannot be known."""
    return _build_revision() or _checkout_revision()


def version_string() -> str:
    """Release number plus short commit, e.g. ``0.2.0 (1a2b3c4)``."""
    commit = revision()
    return f"{__version__} ({commit[:7]})" if commit else f"{__version__} (revision unknown)"
