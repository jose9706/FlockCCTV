"""Local deployment checks; no network, systemd, or Discord connection."""

from __future__ import annotations

import io
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import json
import time

from deploy.update import (
    BACKUP_NAME,
    REVISION_FILE,
    RETRY_FAILED_AFTER_SECONDS,
    RetryDeferred,
    RevisionFailed,
    UpdateError,
    Updater,
)


OLD = "a" * 40
NEW = "b" * 40


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.install = root / "flock-cctv"
        self.install.mkdir()
        (self.install / REVISION_FILE).write_text(OLD + "\n", encoding="ascii")
        (self.install / "old-code").write_text("old", encoding="ascii")
        self.database = root / "tracker.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE tracker(value TEXT)")
            connection.execute("INSERT INTO tracker VALUES ('kept')")
            connection.execute("CREATE TABLE settings(singleton INTEGER PRIMARY KEY, tracking_since REAL)")
            connection.execute("INSERT INTO settings VALUES (1, 1)")
            connection.commit()
        self.backups = root / "backups"
        self.backups.mkdir()
        self.updater = Updater("https://example.invalid/tracker.git", self.install, self.database, self.backups)

    def fake_stage(self, work: Path, branch: str) -> tuple[Path, str]:
        self.assertEqual(branch, "main")
        candidate = work / "candidate"
        candidate.mkdir()
        (candidate / REVISION_FILE).write_text(NEW + "\n", encoding="ascii")
        (candidate / "new-code").write_text("new", encoding="ascii")
        return candidate, NEW

    def interrupted_swap(self, *, candidate_installed: bool = False) -> None:
        original_stat = self.database.stat()
        owner = {
            "uid": original_stat.st_uid,
            "gid": original_stat.st_gid,
            "mode": stat.S_IMODE(original_stat.st_mode),
        }
        backup = self.updater.backup_database()
        self.updater.write_pending(
            owner, verified=False, backup_sha256=self.updater.backup_digest(backup)
        )
        os.replace(self.install, self.updater.previous)
        if candidate_installed:
            self.install.mkdir()
            (self.install / REVISION_FILE).write_text(NEW + "\n", encoding="ascii")
            (self.install / "new-code").write_text("new", encoding="ascii")

    def test_same_revision_does_not_stop_bot(self) -> None:
        with mock.patch.object(self.updater, "remote_head", return_value=("main", OLD)), \
             mock.patch.object(self.updater, "command") as command:
            self.assertFalse(self.updater.update())
        command.assert_not_called()

    def test_success_keeps_previous_code_and_private_database_backup(self) -> None:
        with mock.patch.object(self.updater, "remote_head", return_value=("main", NEW)), \
             mock.patch.object(self.updater, "stage", side_effect=self.fake_stage), \
             mock.patch.object(self.updater, "command") as command, \
             mock.patch.object(self.updater, "start_and_check"):
            self.assertTrue(self.updater.update())
        self.assertTrue((self.install / "new-code").exists())
        self.assertTrue((self.updater.previous / "old-code").exists())
        self.assertTrue((self.backups / BACKUP_NAME).exists())
        command.assert_called_once_with("systemctl", "stop", "flock-cctv.service")

    def test_failed_start_restores_old_code_and_database(self) -> None:
        def start(**_: object) -> None:
            if start.call_count == 0:
                start.call_count += 1
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute("DROP TABLE tracker")
                    connection.commit()
                raise UpdateError("new bot did not stay active")
            start.call_count += 1

        start.call_count = 0
        with mock.patch.object(self.updater, "remote_head", return_value=("main", NEW)), \
             mock.patch.object(self.updater, "stage", side_effect=self.fake_stage), \
             mock.patch.object(self.updater, "command") as command, \
             mock.patch.object(self.updater, "start_and_check", side_effect=start):
            with self.assertRaisesRegex(UpdateError, "new bot did not stay active"):
                self.updater.update()
        self.assertTrue((self.install / "old-code").exists())
        self.assertFalse((self.install / "new-code").exists())
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM tracker").fetchone(), ("kept",))
        self.assertEqual(start.call_count, 2)
        self.assertEqual(command.call_count, 2)

    def test_poll_removes_staging_left_by_killed_update(self) -> None:
        stale = self.install.parent / ".flock-cctv-update-stale"
        (stale / "candidate").mkdir(parents=True)
        with mock.patch.object(self.updater, "remote_head", return_value=("main", OLD)):
            self.assertFalse(self.updater.update())
        self.assertFalse(stale.exists())
        self.assertTrue((self.install / "old-code").exists())

    def test_failed_command_output_reaches_journal(self) -> None:
        with mock.patch("sys.stderr", new=io.StringIO()) as stderr:
            with self.assertRaises(subprocess.CalledProcessError):
                self.updater.command(
                    sys.executable, "-c", "import sys; print('FAILED: test_x'); sys.exit(1)"
                )
        self.assertIn("FAILED: test_x", stderr.getvalue())

    def test_remote_head_must_be_a_symbolic_branch(self) -> None:
        with mock.patch.object(self.updater, "command", return_value=NEW + "\tHEAD"):
            with self.assertRaises(UpdateError):
                self.updater.remote_head()

    def test_health_check_rejects_restart_loop(self) -> None:
        with mock.patch.object(self.updater, "command", side_effect=["0", "", "", "1"]), \
             mock.patch("deploy.update.time.sleep"):
            with self.assertRaisesRegex(UpdateError, "restarted"):
                self.updater.start_and_check(require_ready=False)

    def test_recovery_restores_install_missing_after_interrupted_swap(self) -> None:
        self.interrupted_swap()
        self.assertFalse(self.install.exists())
        self.assertIsNone(self.updater.recover_pending(stop_service=False))
        self.assertTrue((self.install / "old-code").exists())
        self.assertFalse(self.updater.pending.exists())

    def test_recovery_restores_code_and_database_after_candidate_migration(self) -> None:
        self.interrupted_swap(candidate_installed=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE tracker")
            connection.commit()
        self.assertIsNone(self.updater.recover_pending(stop_service=False))
        self.assertTrue((self.install / "old-code").exists())
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM tracker").fetchone(), ("kept",))
        self.assertFalse(self.updater.pending.exists())

    def test_restore_does_not_follow_a_preexisting_temporary_symlink(self) -> None:
        backup = self.updater.backup_database()
        original_stat = self.database.stat()
        owner = {
            "uid": original_stat.st_uid,
            "gid": original_stat.st_gid,
            "mode": stat.S_IMODE(original_stat.st_mode),
        }
        unrelated = self.database.parent / "unrelated-file"
        unrelated.write_text("leave this intact", encoding="ascii")
        temporary = self.database.with_name(self.database.name + ".update-restore")
        temporary.symlink_to(unrelated)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE tracker")
            connection.commit()

        self.updater.restore_database(backup, owner)

        self.assertFalse(self.database.is_symlink())
        self.assertEqual(unrelated.read_text(encoding="ascii"), "leave this intact")
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), owner["mode"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM tracker").fetchone(), ("kept",))

    def test_restore_cleans_up_unique_temporary_file_after_copy_failure(self) -> None:
        backup = self.updater.backup_database()
        original_stat = self.database.stat()
        owner = {
            "uid": original_stat.st_uid,
            "gid": original_stat.st_gid,
            "mode": stat.S_IMODE(original_stat.st_mode),
        }
        sidecars = [
            self.database.with_name(self.database.name + suffix)
            for suffix in ("-wal", "-shm", "-journal")
        ]
        for sidecar in sidecars:
            sidecar.write_bytes(b"existing state")
        with mock.patch("deploy.update.shutil.copyfileobj", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(OSError, "copy failed"):
                self.updater.restore_database(backup, owner)

        self.assertEqual(list(self.database.parent.glob(".*.update-restore-*.tmp")), [])
        for sidecar in sidecars:
            self.assertEqual(sidecar.read_bytes(), b"existing state")
            sidecar.unlink()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM tracker").fetchone(), ("kept",))

    def test_restore_keeps_journals_until_the_copy_is_durable(self) -> None:
        backup = self.updater.backup_database()
        original_stat = self.database.stat()
        owner = {
            "uid": original_stat.st_uid,
            "gid": original_stat.st_gid,
            "mode": stat.S_IMODE(original_stat.st_mode),
        }
        wal = self.database.with_name(self.database.name + "-wal")
        wal.write_bytes(b"existing state")
        with mock.patch("deploy.update.os.fsync", side_effect=OSError("fsync failed")):
            with self.assertRaisesRegex(OSError, "fsync failed"):
                self.updater.restore_database(backup, owner)
        self.assertEqual(wal.read_bytes(), b"existing state")
        self.assertEqual(list(self.database.parent.glob(".*.update-restore-*.tmp")), [])
        wal.unlink()

    def test_hold_file_is_replaced_atomically_and_readable(self) -> None:
        self.updater.set_hold(OLD)
        self.updater.set_hold(NEW)
        self.assertEqual(self.updater.hold.read_text(encoding="ascii"), NEW + "\n")
        self.assertEqual(stat.S_IMODE(self.updater.hold.stat().st_mode), 0o644)
        self.assertEqual(list(self.updater.hold.parent.glob(".flock-cctv-hold-*")), [])

    def test_verified_candidate_is_kept_after_interrupted_cleanup(self) -> None:
        self.interrupted_swap(candidate_installed=True)
        owner, _, digest = self.updater.read_pending()
        self.updater.write_pending(owner, verified=True, backup_sha256=digest)
        self.assertIsNone(self.updater.recover_pending(stop_service=False))
        self.assertTrue((self.install / "new-code").exists())
        self.assertTrue((self.updater.previous / "old-code").exists())
        self.assertFalse(self.updater.pending.exists())

    def test_recovery_does_not_recreate_backup_deleted_by_bot(self) -> None:
        self.interrupted_swap(candidate_installed=True)
        backup = self.backups / BACKUP_NAME
        backup.unlink()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM tracker")
            connection.execute("UPDATE settings SET tracking_since = 2")
            connection.commit()
        warning = self.updater.recover_pending(stop_service=False)
        self.assertIn("does not match pending record", warning)
        self.assertFalse(backup.exists())
        self.assertTrue((self.install / "old-code").exists())

    def test_recovery_refuses_a_stale_backup_with_same_tracking_start(self) -> None:
        self.interrupted_swap(candidate_installed=True)
        backup = self.backups / BACKUP_NAME
        with closing(sqlite3.connect(backup)) as connection:
            connection.execute("UPDATE tracker SET value = 'stale'")
            connection.commit()
        warning = self.updater.recover_pending(stop_service=False)
        self.assertIn("does not match pending record", warning)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM tracker").fetchone(), ("kept",))
        self.assertTrue((self.install / "old-code").exists())

    def test_missing_database_is_not_recreated_by_recovery(self) -> None:
        self.interrupted_swap(candidate_installed=True)
        self.database.unlink()
        with self.assertRaisesRegex(UpdateError, "Database missing after rollback"):
            self.updater.recover_pending(stop_service=False)
        self.assertFalse(self.database.exists())
        self.assertTrue(self.updater.pending.exists())

    def test_rollback_does_not_revive_deleted_statistics(self) -> None:
        def start(**_: object) -> None:
            if start.call_count == 0:
                start.call_count += 1
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute("DELETE FROM tracker")
                    connection.execute("UPDATE settings SET tracking_since = 2")
                    connection.commit()
                raise UpdateError("candidate failed")
            start.call_count += 1

        start.call_count = 0
        with mock.patch.object(self.updater, "remote_head", return_value=("main", NEW)), \
             mock.patch.object(self.updater, "stage", side_effect=self.fake_stage), \
             mock.patch.object(self.updater, "command"), \
             mock.patch.object(self.updater, "start_and_check", side_effect=start):
            with self.assertRaisesRegex(UpdateError, "database was not restored"):
                self.updater.update()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tracker").fetchone(), (0,))
        self.assertTrue((self.install / "old-code").exists())

    def test_status_records_success_failure_streak_and_deferred_polls(self) -> None:
        self.updater.record("current", branch="main")
        status = json.loads(self.updater.status_path.read_text(encoding="utf-8"))
        self.assertEqual((status["result"], status["revision"], status["failures"]), ("current", OLD, 0))
        self.assertEqual(stat.S_IMODE(self.updater.status_path.stat().st_mode), 0o644)

        with mock.patch("deploy.update.time.time", return_value=1000.0):
            self.updater.record("failed", message="boom\n  details", failed_revision=NEW)
        with mock.patch("deploy.update.time.time", return_value=2000.0):
            self.updater.record("failed", message="boom again", failed_revision=NEW)
        status = self.updater.read_status()
        self.assertEqual((status["failures"], status["failing_since"]), (2, 1000.0))
        self.assertEqual(status["failed_revision"], NEW)
        with mock.patch("deploy.update.time.time", return_value=3000.0):
            self.updater.record("failed", attempted=False)
        status = self.updater.read_status()
        self.assertEqual((status["failures"], status["attempted_at"], status["message"]), (2, 2000.0, "boom again"))
        self.assertEqual(status["checked_at"], 3000.0)

        self.updater.record("updated", branch="main")
        status = self.updater.read_status()
        self.assertEqual((status["failures"], status["failing_since"], status["failed_revision"]), (0, None, None))
        self.updater.record("failed", message="x" * 1000)
        self.assertEqual(len(self.updater.read_status()["message"]), 300)

    def test_recently_failed_commit_is_not_restaged_until_retry_window(self) -> None:
        self.updater.record("failed", message="tests failed", failed_revision=NEW)
        with mock.patch.object(self.updater, "remote_head", return_value=("main", NEW)), \
             mock.patch.object(self.updater, "stage") as stage:
            with self.assertRaises(RetryDeferred):
                self.updater.update()
            stage.assert_not_called()
            later = time.time() + RETRY_FAILED_AFTER_SECONDS + 1
            stage.side_effect = UpdateError("tests failed again")
            with mock.patch("deploy.update.time.time", return_value=later), \
                 self.assertRaises(RevisionFailed) as raised:
                self.updater.update()
        self.assertEqual(raised.exception.revision, NEW)
        self.assertTrue((self.install / "old-code").exists())

    def test_target_revision_deploys_without_polling_and_hold_round_trips(self) -> None:
        with mock.patch.object(self.updater, "remote_head") as remote_head, \
             mock.patch.object(self.updater, "stage", side_effect=lambda work, ref: (
                 self.assertEqual(ref, NEW) or self.fake_stage(work, "main"))), \
             mock.patch.object(self.updater, "command"), \
             mock.patch.object(self.updater, "start_and_check"):
            self.assertTrue(self.updater.update(target=NEW))
        remote_head.assert_not_called()
        self.assertEqual(self.updater.deployed_revision(), NEW)
        with self.assertRaisesRegex(UpdateError, "full commit"):
            self.updater.update(target="abc123")
        self.updater.set_hold(NEW)
        self.assertEqual(self.updater.held_revision(), NEW)
        self.updater.set_hold(None)
        self.assertIsNone(self.updater.held_revision())

    def test_request_from_bot_is_consumed_and_unit_watches_the_same_file(self) -> None:
        from flock_cctv import update_status

        update_status.request_update(self.database)
        self.assertTrue(self.updater.request_path.exists())
        self.updater.clear_request()
        self.assertFalse(self.updater.request_path.exists())
        self.updater.clear_request()  # A timer poll with no request is fine.
        unit = (Path(__file__).parents[1] / "deploy" / "flock-cctv-update.path").read_text(encoding="utf-8")
        self.assertIn(
            f"PathExists=/var/lib/flock-cctv/{update_status.REQUEST_NAME}\n", unit
        )
        self.assertIn("Unit=flock-cctv-update.service", unit)

    def test_health_check_waits_for_a_fresh_discord_ready_marker(self) -> None:
        clock = iter(range(0, 10_000, 5))
        with mock.patch.object(self.updater, "command", side_effect=["0", "", "", "0"]), \
             mock.patch("deploy.update.time.time", return_value=500.0), \
             mock.patch("deploy.update.time.monotonic", side_effect=lambda: float(next(clock))), \
             mock.patch("deploy.update.time.sleep"):
            # A marker from before this start does not count.
            self.updater.ready_path.write_text(json.dumps({"ready_at": 499.0}), encoding="utf-8")
            with self.assertRaisesRegex(UpdateError, "did not connect to Discord"):
                self.updater.start_and_check()

        def write_marker(*_: object) -> None:
            self.updater.ready_path.write_text(json.dumps({"ready_at": 501.0}), encoding="utf-8")

        with mock.patch.object(self.updater, "command", side_effect=["0", "", "", "0"]), \
             mock.patch("deploy.update.time.time", return_value=500.0), \
             mock.patch("deploy.update.time.sleep", side_effect=write_marker):
            self.updater.start_and_check()

    def test_staged_code_runs_as_unprivileged_user(self) -> None:
        self.updater.stage_user = "flock-cctv"
        with mock.patch.object(self.updater, "command", return_value="") as command:
            self.updater.staged_command("python", "-m", "unittest", cwd=self.install, home=self.install, timeout=5)
        args = command.call_args.args
        self.assertEqual(args[:4], ("runuser", "-u", "flock-cctv", "--"))
        self.assertIn(f"HOME={self.install}", args)
        self.assertEqual(args[-3:], ("python", "-m", "unittest"))

    def test_hardening_strips_setuid_and_group_world_write(self) -> None:
        tree = self.install.parent / "staged"
        (tree / "bin").mkdir(parents=True)
        tool = tree / "bin" / "tool"
        tool.write_text("x", encoding="ascii")
        os.chmod(tool, 0o4777)
        (tree / "link").symlink_to("/etc/passwd")
        Updater.chown_tree(tree, os.getuid(), os.getgid(), harden=True)
        self.assertEqual(stat.S_IMODE(tool.stat().st_mode), 0o755)
        self.assertTrue((tree / "link").is_symlink())


if __name__ == "__main__":
    unittest.main()
