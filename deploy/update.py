"""Pull and install the remote default branch on the Raspberry Pi.

This is run from /usr/local/libexec as root. The bot itself continues
to run as the unprivileged flock-cctv account.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import date
from pathlib import Path


REVISION_FILE = ".deployed-revision"
BUILD_STAMP = "src/flock_cctv/_build.py"
BACKUP_NAME = "flock-cctv-before-update.sqlite3"
PENDING_NAME = ".flock-cctv.update-pending.json"
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
WORK_PREFIX = ".flock-cctv-update-"
OUTPUT_TAIL_LINES = 40
# Written next to the database: the bot reads the status and writes the ready marker.
STATUS_NAME = "update-status.json"
READY_NAME = "bot-ready.json"
HOLD_NAME = ".flock-cctv.hold"
# Written by the bot's /flock update; flock-cctv-update.path starts a poll.
REQUEST_NAME = "update-requested.json"
READY_TIMEOUT_SECONDS = 180
SETTLE_SECONDS = 15
RETRY_FAILED_AFTER_SECONDS = 6 * 60 * 60
MESSAGE_LIMIT = 300


class UpdateError(RuntimeError):
    pass


class RevisionFailed(UpdateError):
    """An update of a known commit failed; polling waits before retrying it."""

    def __init__(self, revision: str, message: str) -> None:
        super().__init__(message)
        self.revision = revision


class RetryDeferred(UpdateError):
    """The remote commit failed recently and is not retried yet."""


class Updater:
    def __init__(
        self,
        repository: str,
        install: Path = Path("/opt/flock-cctv"),
        database: Path = Path("/var/lib/flock-cctv/tracker.sqlite3"),
        backups: Path = Path("/var/lib/flock-cctv/backups"),
        service: str = "flock-cctv.service",
        stage_user: str | None = None,
    ) -> None:
        self.repository = repository
        self.install = install
        self.previous = install.with_name(install.name + ".previous")
        self.pending = install.with_name(PENDING_NAME)
        self.hold = install.with_name(HOLD_NAME)
        self.database = database
        self.backups = backups
        self.service = service
        self.stage_user = stage_user
        self.branch: str | None = None
        self.status_path = database.with_name(STATUS_NAME)
        self.ready_path = database.with_name(READY_NAME)
        self.request_path = database.with_name(REQUEST_NAME)

    @staticmethod
    def command(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run(
                args, cwd=cwd, env=env, check=True, capture_output=True,
                text=True, timeout=timeout,
            )
        except subprocess.CalledProcessError as exc:
            # Keep the tail of a failed install or test run in the journal.
            output = "\n".join(part for part in (exc.stdout, exc.stderr) if part)
            if output:
                print("\n".join(output.splitlines()[-OUTPUT_TAIL_LINES:]), file=sys.stderr)
            raise
        return result.stdout.strip()

    def remote_head(self) -> tuple[str, str]:
        output = self.command("git", "ls-remote", "--symref", self.repository, "HEAD", timeout=60)
        branch = None
        revision = None
        for line in output.splitlines():
            if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
                branch = line.removeprefix("ref: refs/heads/").removesuffix("\tHEAD")
            elif line.endswith("\tHEAD"):
                revision = line.removesuffix("\tHEAD")
        if not branch or not revision or not SHA_RE.fullmatch(revision):
            raise UpdateError("Remote HEAD is not a valid branch and commit")
        return branch, revision

    def staged_command(self, *args: str, cwd: Path, home: Path, timeout: int) -> str:
        """Run repository code as the unprivileged staging user, never as root."""
        if self.stage_user is None:
            return self.command(*args, cwd=cwd, timeout=timeout)
        return self.command(
            "runuser", "-u", self.stage_user, "--",
            "env", f"HOME={home}", "PIP_NO_CACHE_DIR=1", *args,
            cwd=cwd, timeout=timeout,
        )

    @staticmethod
    def chown_tree(root: Path, uid: int, gid: int, *, harden: bool = False) -> None:
        """Change ownership without following symlinks; optionally drop risky mode bits."""
        for directory, directories, files in os.walk(root, followlinks=False):
            for name in (*directories, *files):
                item = Path(directory) / name
                os.lchown(item, uid, gid)
                if harden and not item.is_symlink():
                    # Staged files must not keep setuid/setgid or group/world write.
                    os.chmod(item, stat.S_IMODE(item.lstat().st_mode) & ~0o6022)
        os.lchown(root, uid, gid)
        if harden:
            os.chmod(root, stat.S_IMODE(root.lstat().st_mode) & ~0o6022)

    def stage(self, work: Path, ref: str) -> tuple[Path, str]:
        """Fetch ``ref`` (a branch or commit), install, and test it in ``work``."""
        checkout = work / "checkout"
        candidate = work / "candidate"
        # Git only copies files here; no repository code runs as root.
        self.command("git", "init", "--quiet", str(checkout), timeout=60)
        self.command(
            "git", "-C", str(checkout), "fetch", "--quiet", "--depth=1", "--no-tags",
            "--", self.repository, ref, timeout=300,
        )
        self.command("git", "-C", str(checkout), "checkout", "--quiet", "--detach", "FETCH_HEAD")
        revision = self.command("git", "rev-parse", "HEAD", cwd=checkout)
        if not SHA_RE.fullmatch(revision):
            raise UpdateError("Checkout did not produce a valid commit")
        shutil.copytree(
            checkout, candidate,
            ignore=shutil.ignore_patterns(".git", ".venv", ".env", "data", "*.sqlite*"),
        )
        (candidate / REVISION_FILE).write_text(revision + "\n", encoding="ascii")
        # Installed packages cannot see the tree they came from, so stamp the
        # commit into the package for `--version` and `/flock about`.
        (candidate / BUILD_STAMP).write_text(f'REVISION = "{revision}"\n', encoding="ascii")
        if self.stage_user is not None:
            account = pwd.getpwnam(self.stage_user)
            os.chmod(work, 0o711)
            self.chown_tree(candidate, account.pw_uid, account.pw_gid)
        python = str(candidate / ".venv/bin/python")
        self.staged_command(sys.executable, "-m", "venv", str(candidate / ".venv"), cwd=candidate, home=candidate, timeout=600)
        self.staged_command(
            python, "-m", "pip", "install", "-c", "requirements.lock", ".",
            cwd=candidate, home=candidate, timeout=900,
        )
        self.staged_command(
            python, "-m", "unittest", "discover", "-s", "tests", "-v",
            cwd=candidate, home=candidate, timeout=300,
        )
        if self.stage_user is not None:
            self.chown_tree(candidate, 0, 0, harden=True)
        # Persist staged files before the pending record can authorize a swap.
        self.sync_tree(candidate)
        return candidate, revision

    def backup_database(self) -> Path:
        if not self.database.is_file() or not self.backups.is_dir():
            raise UpdateError("Configured database or backup directory is missing")
        backup = self.backups / BACKUP_NAME
        fd, temp_name = tempfile.mkstemp(
            prefix=f".flock-cctv-{date.today().isoformat()}.",
            suffix=".sqlite3.tmp", dir=self.backups,
        )
        os.close(fd)
        temp = Path(temp_name)
        try:
            source = sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                destination = sqlite3.connect(temp)
                try:
                    source.backup(destination)
                    result = destination.execute("PRAGMA integrity_check").fetchone()
                    if result != ("ok",):
                        raise UpdateError("Database backup failed integrity check")
                finally:
                    destination.close()
            finally:
                source.close()
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temp, backup)
            self.sync_directory(self.backups)
            return backup
        finally:
            temp.unlink(missing_ok=True)

    def restore_database(self, backup: Path, owner: dict[str, int]) -> None:
        # The new code may have migrated the database before failing to start.
        # Restore the matching pre-update state only after the new bot is stopped.
        for suffix in ("-wal", "-shm", "-journal"):
            self.database.with_name(self.database.name + suffix).unlink(missing_ok=True)
        temp = self.database.with_name(self.database.name + ".update-restore")
        try:
            shutil.copyfile(backup, temp)
            os.chown(temp, owner["uid"], owner["gid"])
            os.chmod(temp, owner["mode"])
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temp, self.database)
            self.sync_directory(self.database.parent)
        finally:
            temp.unlink(missing_ok=True)

    def data_was_deleted(self, backup: Path) -> bool:
        """Never revive statistics erased while the candidate was starting."""
        if not backup.is_file():
            raise UpdateError("Pre-update backup was removed; database left intact")
        if not self.database.is_file():
            raise UpdateError("Database is missing; database left intact")
        try:
            with closing(sqlite3.connect(backup.resolve().as_uri() + "?mode=ro", uri=True)) as before, \
                 closing(sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)) as after:
                old_start = before.execute("SELECT tracking_since FROM settings WHERE singleton = 1").fetchone()
                new_start = after.execute("SELECT tracking_since FROM settings WHERE singleton = 1").fetchone()
                return old_start != new_start
        except sqlite3.Error as exc:
            raise UpdateError("Cannot establish whether data was deleted; database left intact") from exc

    @staticmethod
    def sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def sync_tree(cls, root: Path) -> None:
        for directory, _, files in os.walk(root, topdown=False, followlinks=False):
            path = Path(directory)
            for name in files:
                item = path / name
                if item.is_symlink() or not item.is_file():
                    continue
                with item.open("rb") as stream:
                    os.fsync(stream.fileno())
            cls.sync_directory(path)

    @staticmethod
    def backup_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def write_pending(self, owner: dict[str, int], *, verified: bool, backup_sha256: str) -> None:
        """Persist recovery state before moving either code directory."""
        descriptor, temp_name = tempfile.mkstemp(prefix=".flock-cctv-pending-", dir=self.install.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                json.dump({**owner, "verified": verified, "backup_sha256": backup_sha256}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.pending)
            self.sync_directory(self.install.parent)
        finally:
            temp.unlink(missing_ok=True)

    def read_pending(self) -> tuple[dict[str, int], bool, str]:
        try:
            value = json.loads(self.pending.read_text(encoding="ascii"))
            if not isinstance(value, dict):
                raise ValueError("not an object")
            for key in ("uid", "gid", "mode"):
                if type(value.get(key)) is not int or value[key] < 0:
                    raise ValueError(f"invalid {key}")
            if type(value.get("verified")) is not bool:
                raise ValueError("invalid verified flag")
            digest = value.get("backup_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("invalid backup digest")
            return {key: value[key] for key in ("uid", "gid", "mode")}, value["verified"], digest
        except (OSError, ValueError) as exc:
            raise UpdateError("Invalid pending update record; manual recovery required") from exc

    def clear_pending(self) -> None:
        self.pending.unlink()
        self.sync_directory(self.install.parent)

    def recover_pending(self, *, stop_service: bool) -> str | None:
        """Recover an interrupted swap before the bot can start on reboot."""
        if not self.pending.exists():
            return None
        owner, verified, expected_digest = self.read_pending()
        if verified and self.install.is_dir() and not self.install.is_symlink():
            if not self.database.is_file():
                raise UpdateError("Database missing after verified update; manual recovery required")
            self.clear_pending()
            return None
        warning = None
        if self.previous.exists() or self.previous.is_symlink():
            if self.previous.is_symlink() or not self.previous.is_dir():
                raise UpdateError("Unexpected previous install during recovery")
            if stop_service:
                self.command("systemctl", "stop", self.service)
            backup = self.backups / BACKUP_NAME
            try:
                if not backup.is_file() or self.backup_digest(backup) != expected_digest:
                    raise UpdateError("Pre-update backup does not match pending record; database left intact")
                if self.data_was_deleted(backup):
                    warning = "Tracking data was deleted; pre-update database was not restored"
            except UpdateError as exc:
                warning = str(exc)
            if warning is None:
                self.restore_database(backup, owner)
            if self.install.exists() or self.install.is_symlink():
                if not self.install.is_dir() or self.install.is_symlink():
                    raise UpdateError("Unexpected installed path during recovery")
                shutil.rmtree(self.install)
            os.replace(self.previous, self.install)
        elif not self.install.is_dir() or self.install.is_symlink():
            raise UpdateError("Installed code and previous code are both missing")
        if not self.database.is_file():
            raise UpdateError("Database missing after rollback; manual recovery required")
        self.clear_pending()
        return warning

    def clear_request(self) -> None:
        """Consume an on-demand request so the path unit does not start another poll.

        A request written after this point starts one more poll once this run ends.
        """
        try:
            self.request_path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Could not clear the update request: {exc}", file=sys.stderr)

    def remove_stale_work(self) -> None:
        """Remove staging trees left by an updater killed mid-stage."""
        for path in self.install.parent.glob(WORK_PREFIX + "*"):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)

    def ready_since(self, started: float) -> bool:
        """True when the bot reported a Discord connection after ``started``."""
        try:
            ready_at = json.loads(self.ready_path.read_text(encoding="utf-8"))["ready_at"]
            return isinstance(ready_at, (int, float)) and ready_at >= started
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def start_and_check(self, *, require_ready: bool = True) -> None:
        """Start the bot and require it to stay up and, by default, reach Discord.

        Rollbacks skip the Discord check: older code may not write the marker.
        """
        before = int(self.command("systemctl", "show", "--property=NRestarts", "--value", self.service))
        started = time.time()
        self.command("systemctl", "start", self.service)
        if require_ready:
            deadline = time.monotonic() + READY_TIMEOUT_SECONDS
            while not self.ready_since(started):
                if time.monotonic() >= deadline:
                    raise UpdateError(
                        f"Bot did not connect to Discord within {READY_TIMEOUT_SECONDS} seconds"
                    )
                time.sleep(3)
        time.sleep(SETTLE_SECONDS)
        self.command("systemctl", "is-active", "--quiet", self.service)
        after = int(self.command("systemctl", "show", "--property=NRestarts", "--value", self.service))
        if after != before:
            raise UpdateError("Bot restarted during activation check")

    def read_status(self) -> dict:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def deployed_revision(self) -> str | None:
        try:
            revision = (self.install / REVISION_FILE).read_text(encoding="ascii").strip()
        except OSError:
            return None
        return revision if SHA_RE.fullmatch(revision) else None

    def held_revision(self) -> str | None:
        try:
            revision = self.hold.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        if not SHA_RE.fullmatch(revision):
            raise UpdateError(f"Invalid hold file at {self.hold}")
        return revision

    def set_hold(self, revision: str | None) -> None:
        if revision is None:
            self.hold.unlink(missing_ok=True)
        else:
            self.hold.write_text(revision + "\n", encoding="ascii")
        self.sync_directory(self.hold.parent)

    def record(
        self,
        result: str,
        *,
        message: str = "",
        branch: str | None = None,
        failed_revision: str | None = None,
        attempted: bool = True,
    ) -> None:
        """Write the outcome the bot shows in /flock about and alerts on.

        ``result`` is ``current``, ``updated``, ``held``, or ``failed``. A
        failure that was not attempted again keeps the streak's first time.
        """
        previous = self.read_status()
        now = time.time()
        status = {
            "checked_at": now,
            "result": result,
            "branch": branch or previous.get("branch"),
            "revision": self.deployed_revision(),
            "message": " ".join(message.split())[:MESSAGE_LIMIT],
            "last_success_at": previous.get("last_success_at"),
            "failures": 0,
            "failing_since": None,
            "failed_revision": None,
            "attempted_at": now,
        }
        if result == "failed":
            failing = previous.get("result") == "failed"
            status["failing_since"] = previous.get("failing_since") if failing else now
            if attempted:
                status["failures"] = int(previous.get("failures") or 0) + 1 if failing else 1
                status["failed_revision"] = failed_revision
            else:
                for key in ("failures", "failed_revision", "attempted_at", "message"):
                    status[key] = previous.get(key)
        else:
            status["last_success_at"] = now
        descriptor, temp_name = tempfile.mkstemp(prefix=".update-status-", dir=self.status_path.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(status, stream)
            os.chmod(temp, 0o644)  # The unprivileged bot reads it; it holds no secrets.
            os.replace(temp, self.status_path)
        finally:
            temp.unlink(missing_ok=True)

    def update(self, target: str | None = None) -> bool:
        """Deploy the remote default branch, or ``target`` commit when given."""
        if self.pending.exists():
            raise UpdateError("Interrupted update must be recovered before polling")
        if not self.install.is_dir() or self.install.is_symlink():
            raise UpdateError(f"Expected an installed directory at {self.install}")
        self.remove_stale_work()
        if target is not None:
            if not SHA_RE.fullmatch(target):
                raise UpdateError("--revision must be a full commit ID")
            branch, remote_revision = "pinned", target
        else:
            branch, remote_revision = self.remote_head()
        self.branch = branch
        deployed = self.install / REVISION_FILE
        if deployed.is_file() and deployed.read_text(encoding="ascii").strip() == remote_revision:
            print(f"Already at {remote_revision} ({branch})")
            return False
        status = self.read_status()
        if (
            target is None
            and status.get("failed_revision") == remote_revision
            and time.time() - float(status.get("attempted_at") or 0) < RETRY_FAILED_AFTER_SECONDS
        ):
            raise RetryDeferred(
                f"{remote_revision[:12]} failed recently; retrying it after "
                f"{RETRY_FAILED_AFTER_SECONDS // 3600} hours or on a new commit"
            )
        try:
            return self.install_revision(remote_revision, branch, ref=target or branch)
        except RevisionFailed:
            raise
        except Exception as exc:
            raise RevisionFailed(remote_revision, str(exc) or type(exc).__name__) from exc

    def install_revision(self, remote_revision: str, branch: str, *, ref: str) -> bool:
        deployed = self.install / REVISION_FILE
        with tempfile.TemporaryDirectory(prefix=WORK_PREFIX, dir=self.install.parent) as work_name:
            work = Path(work_name)
            candidate, revision = self.stage(work, ref)
            # A concurrent push during clone is fine: install the actual checkout.
            if deployed.is_file() and deployed.read_text(encoding="ascii").strip() == revision:
                print(f"Already at {revision} ({branch})")
                return False

            # The current bot keeps running while the new dependencies and tests
            # are prepared. Stop it before taking a backup or changing code.
            self.command("systemctl", "stop", self.service)
            try:
                original_stat = self.database.stat()
                owner = {
                    "uid": original_stat.st_uid,
                    "gid": original_stat.st_gid,
                    "mode": stat.S_IMODE(original_stat.st_mode),
                }
                backup = self.backup_database()
                backup_sha256 = self.backup_digest(backup)
                if self.previous.exists():
                    if self.previous.is_symlink() or not self.previous.is_dir():
                        raise UpdateError(f"Unexpected previous install at {self.previous}")
                    shutil.rmtree(self.previous)
                self.write_pending(owner, verified=False, backup_sha256=backup_sha256)
                os.replace(self.install, self.previous)
                self.sync_directory(self.install.parent)
                os.replace(candidate, self.install)
                self.sync_directory(self.install.parent)
                self.start_and_check()
                self.write_pending(owner, verified=True, backup_sha256=backup_sha256)
                self.clear_pending()
            except Exception as original_error:
                try:
                    warning = self.recover_pending(stop_service=True)
                    self.start_and_check(require_ready=False)
                except Exception as recovery_error:
                    raise UpdateError(f"Update failed and recovery failed: {recovery_error}") from original_error
                if warning:
                    raise UpdateError(f"Update failed; {warning}") from original_error
                raise

            print(f"Updated {self.service} to {revision} ({branch}); previous code: {self.previous}")
            return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", help="Git URL whose default branch is deployed")
    parser.add_argument("--recover-only", action="store_true", help="recover before starting the bot")
    parser.add_argument(
        "--revision", help="deploy this full commit ID and hold polling there (manual rollback)"
    )
    parser.add_argument(
        "--release", action="store_true", help="clear a --revision hold and resume deploying the default branch"
    )
    parser.add_argument(
        "--stage-user", default="flock-cctv",
        help="unprivileged account that installs and tests staged code when run as root",
    )
    parser.add_argument("--install", type=Path, default=Path("/opt/flock-cctv"))
    parser.add_argument("--database", type=Path, default=Path("/var/lib/flock-cctv/tracker.sqlite3"))
    parser.add_argument("--backups", type=Path, default=Path("/var/lib/flock-cctv/backups"))
    args = parser.parse_args()
    if not args.recover_only and not args.repository:
        parser.error("--repository is required when polling")
    # Staged code and virtualenv must be readable by the unprivileged bot.
    # SQLite snapshots use mkstemp (0600) and remain private.
    os.umask(0o022)
    stage_user = args.stage_user if os.geteuid() == 0 else None
    updater = Updater(args.repository or "", args.install, args.database, args.backups, stage_user=stage_user)

    def record(result: str, **fields: object) -> None:
        try:
            updater.record(result, **fields)
        except OSError as exc:
            print(f"Could not write update status: {exc}", file=sys.stderr)

    if not args.recover_only:
        # Clear before taking the lock, so a busy lock cannot make the path unit
        # restart a failing run in a loop.
        updater.clear_request()
    try:
        with open("/run/flock-cctv-update.lock", "w", encoding="ascii") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if args.recover_only:
                    return 0  # The running updater owns recovery and activation.
                raise UpdateError("Another update or recovery is already running")
            if args.recover_only:
                interrupted = updater.pending.exists()
                warning = updater.recover_pending(stop_service=False)
                if warning:
                    print(f"Recovery warning: {warning}", file=sys.stderr)
                if interrupted:
                    record("failed", message=f"Recovered an interrupted update. {warning or ''}")
            elif updater.pending.exists():
                warning = updater.recover_pending(stop_service=True)
                updater.start_and_check(require_ready=False)
                if warning:
                    print(f"Recovery warning: {warning}", file=sys.stderr)
                print("Recovered interrupted update; next timer run will poll")
                record("failed", message=f"Recovered an interrupted update. {warning or ''}")
            else:
                if args.release:
                    updater.set_hold(None)
                    print("Released the hold; the default branch deploys on this and later polls")
                if args.revision:
                    updater.update(target=args.revision)
                    updater.set_hold(args.revision)
                    print(f"Holding at {args.revision}; run with --release to resume updates")
                    record("held", message=f"Held at {args.revision[:12]} by an operator", branch="pinned")
                elif (held := updater.held_revision()) is not None:
                    print(f"Holding at {held}; not polling. Run with --release to resume updates")
                    record("held", message=f"Held at {held[:12]} by an operator", branch="pinned")
                else:
                    updated = updater.update()
                    record("updated" if updated else "current", branch=updater.branch)
    except RetryDeferred as exc:
        print(f"Update skipped: {exc}", file=sys.stderr)
        record("failed", attempted=False)
        return 1
    except (OSError, subprocess.SubprocessError, UpdateError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        record("failed", message=str(exc), failed_revision=getattr(exc, "revision", None))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
