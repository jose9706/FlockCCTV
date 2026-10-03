from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flock_cctv.config import Config


class ConfigTests(unittest.TestCase):
    def _env(self, root: Path, **overrides: str) -> dict[str, str]:
        env = {
            "DISCORD_TOKEN": "test.token.value",
            "GUILD_ID": "123456789",
            "TARGET_USER_ID": "234567891",
            "OWNER_USER_ID": "345678912",
            "DATABASE_PATH": str(root / "data" / "tracker.sqlite3"),
            "BACKUP_DIR": str(root / "data" / "backups"),
        }
        env.update(overrides)
        return env

    def test_defaults_to_visible_channels_and_no_public_output_channel(self):
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, self._env(Path(directory)), clear=True):
                config = Config.from_env()

        self.assertEqual(config.guild_id, 123456789)
        self.assertEqual(config.target_user_id, 234567891)
        self.assertEqual(config.owner_user_id, 345678912)
        self.assertEqual(config.admin_user_ids, frozenset())
        self.assertIsNone(config.text_channel_ids)
        self.assertIsNone(config.voice_channel_ids)
        self.assertIsNone(config.output_channel_id)
        self.assertEqual(config.public_report_channel_ids, frozenset())
        self.assertEqual(config.timezone, "America/Costa_Rica")
        self.assertEqual(config.checkpoint_seconds, 60)
        self.assertEqual(config.retention_days, 90)
        self.assertNotIn("test.token.value", repr(config))

    def test_parses_explicit_channels_and_optional_output(self):
        with TemporaryDirectory() as directory:
            values = self._env(
                Path(directory),
                OUTPUT_CHANNEL_ID="345678912",
                TEXT_CHANNEL_IDS=" 345, 456 ",
                VOICE_CHANNEL_IDS="789",
                TIMEZONE="UTC",
                CHECKPOINT_SECONDS="15",
                RETENTION_DAYS="30",
                ADMIN_USER_IDS=" 456, 789 ",
                PUBLIC_REPORT_CHANNEL_IDS=" 111, 222 ",
            )
            with patch.dict(os.environ, values, clear=True):
                config = Config.from_env()

        self.assertEqual(config.output_channel_id, 345678912)
        self.assertEqual(config.text_channel_ids, frozenset({345, 456}))
        self.assertEqual(config.voice_channel_ids, frozenset({789}))
        self.assertEqual(config.timezone, "UTC")
        self.assertEqual(config.checkpoint_seconds, 15)
        self.assertEqual(config.retention_days, 30)
        self.assertEqual(config.admin_user_ids, frozenset({456, 789}))
        self.assertEqual(config.public_report_channel_ids, frozenset({111, 222}))

    def test_public_report_channels_support_wildcard_and_reject_bad_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, self._env(root, PUBLIC_REPORT_CHANNEL_IDS="*"), clear=True):
                self.assertIsNone(Config.from_env().public_report_channel_ids)
            for invalid in ("123,", "abc", "0"):
                with patch.dict(
                    os.environ,
                    self._env(root, PUBLIC_REPORT_CHANNEL_IDS=invalid),
                    clear=True,
                ):
                    with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                        Config.from_env()

    def test_wildcard_is_none_and_channel_lists_reject_empty_or_bad_ids(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("TEXT_CHANNEL_IDS", "VOICE_CHANNEL_IDS"):
                values = self._env(root, **{name: "*"})
                with patch.dict(os.environ, values, clear=True):
                    self.assertIsNone(getattr(Config.from_env(), name.lower()))
                for invalid in ("", "   ", ",", "123,", "abc", "0", "-2"):
                    values = self._env(root, **{name: invalid})
                    with patch.dict(os.environ, values, clear=True):
                        with self.subTest(name=name, invalid=invalid):
                            with self.assertRaises(ValueError):
                                Config.from_env()

    def test_rejects_missing_invalid_and_unsafe_configuration(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_sets = [
                self._env(root, DISCORD_TOKEN=""),
                self._env(root, DISCORD_TOKEN="has whitespace"),
                self._env(root, GUILD_ID="0"),
                self._env(root, TARGET_USER_ID="-1"),
                self._env(root, OWNER_USER_ID=""),
                self._env(root, OWNER_USER_ID="nope"),
                self._env(root, ADMIN_USER_IDS="12,"),
                self._env(root, ADMIN_USER_IDS="-1"),
                self._env(root, OWNER_USER_ID="234567891"),
                self._env(root, ADMIN_USER_IDS="234567891"),
                self._env(root, OUTPUT_CHANNEL_ID="nope"),
                self._env(root, TIMEZONE="Mars/Olympus_Mons"),
                self._env(root, CHECKPOINT_SECONDS="0"),
                self._env(root, RETENTION_DAYS="-4"),
                self._env(
                    root,
                    DATABASE_PATH=str(root / "data" / "backups" / "db.sqlite3"),
                ),
            ]
            for values in invalid_sets:
                with patch.dict(os.environ, values, clear=True):
                    with self.subTest(values=values):
                        with self.assertRaises(ValueError):
                            Config.from_env()

    def test_required_values_are_required(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "token"}, clear=True):
            with self.assertRaisesRegex(ValueError, "GUILD_ID"):
                Config.from_env()

    def test_owner_is_required_even_with_extra_admins(self):
        with TemporaryDirectory() as directory:
            values = self._env(Path(directory), OWNER_USER_ID="", ADMIN_USER_IDS="456")
            with patch.dict(os.environ, values, clear=True):
                with self.assertRaisesRegex(ValueError, "OWNER_USER_ID"):
                    Config.from_env()


if __name__ == "__main__":
    unittest.main()
