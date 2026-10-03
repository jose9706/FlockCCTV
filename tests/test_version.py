"""Version reporting; no network, systemd, or Discord connection."""

from __future__ import annotations

import contextlib
import io
import re
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import flock_cctv
from flock_cctv.__main__ import main
from deploy.update import BUILD_STAMP, Updater

REVISION = "c" * 40


class VersionTests(unittest.TestCase):
    def test_release_number_is_semantic_and_single_sourced(self) -> None:
        self.assertRegex(flock_cctv.__version__, r"\d+\.\d+\.\d+")
        project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
        self.assertNotIn("version", project["project"])
        self.assertIn("version", project["project"]["dynamic"])

    def test_stamped_revision_is_reported_with_short_commit(self) -> None:
        with mock.patch.dict(sys.modules, {"flock_cctv._build": SimpleNamespace(REVISION=REVISION)}), \
             mock.patch.object(flock_cctv, "_build", SimpleNamespace(REVISION=REVISION), create=True):
            self.assertEqual(flock_cctv.revision(), REVISION)
            self.assertEqual(flock_cctv.version_string(), f"{flock_cctv.__version__} (ccccccc)")

    def test_malformed_stamp_is_ignored(self) -> None:
        with mock.patch.dict(sys.modules, {"flock_cctv._build": SimpleNamespace(REVISION="not-a-commit")}), \
             mock.patch.object(flock_cctv, "_build", SimpleNamespace(REVISION="not-a-commit"), create=True), \
             mock.patch.object(flock_cctv, "_checkout_revision", return_value=None):
            self.assertIsNone(flock_cctv.revision())
            self.assertIn("revision unknown", flock_cctv.version_string())

    def test_version_flag_prints_and_exits_without_configuration(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_info:
            main(["--version"])
        self.assertEqual(exit_info.exception.code, 0)
        self.assertRegex(output.getvalue(), rf"flock-cctv {re.escape(flock_cctv.__version__)} \(")

    def test_updater_stamps_the_staged_package_with_its_commit(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            updater = Updater("https://example.invalid/tracker.git", root / "install")

            fetched: list[str] = []

            def fake_command(*args: str, **kwargs: object) -> str:
                if args[:2] == ("git", "init"):
                    checkout = Path(args[-1])
                    (checkout / "src/flock_cctv").mkdir(parents=True)
                    return ""
                if "fetch" in args:
                    fetched.append(args[-1])
                    return ""
                if args[:2] == ("git", "rev-parse"):
                    return REVISION
                return ""

            with mock.patch.object(Updater, "command", side_effect=fake_command), \
                 mock.patch.object(Updater, "sync_tree"):
                work = root / "work"
                work.mkdir()
                candidate, revision = updater.stage(work, "main")
            self.assertEqual(revision, REVISION)
            self.assertEqual(fetched, ["main"])
            self.assertEqual(
                (candidate / BUILD_STAMP).read_text(encoding="ascii"), f'REVISION = "{REVISION}"\n'
            )


if __name__ == "__main__":
    unittest.main()
