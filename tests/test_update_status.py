"""Bot-side reading of the Pi updater status; no Discord connection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

from flock_cctv import update_status
from flock_cctv.bot import TrackerClient


def failing(since: float = 1_700_000_000.0, failures: int = 3) -> dict:
    return {
        "checked_at": since + 3600, "result": "failed", "revision": "a" * 40,
        "message": "Command 'git ls-remote' returned non-zero exit status 128.",
        "failures": failures, "failing_since": since,
    }


class UpdateStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "tracker.sqlite3"

    def test_status_line_describes_each_result(self) -> None:
        zone = "America/Costa_Rica"
        self.assertIn("no status recorded", update_status.status_line(None, zone))
        line = update_status.status_line(failing(), zone)
        self.assertIn("**failing** since Nov 14, 2023 16:13 CST (3 attempts", line)
        self.assertIn("exit status 128", line)
        current = {"result": "current", "revision": "b" * 40, "checked_at": 1_700_000_000.0}
        self.assertIn("**up to date** at `bbbbbbb`", update_status.status_line(current, zone))
        held = dict(current, result="held")
        self.assertIn("**held** at `bbbbbbb`", update_status.status_line(held, zone))
        updated = dict(current, result="updated")
        self.assertIn("**updated** to `bbbbbbb`", update_status.status_line(updated, zone))

    def test_read_status_ignores_missing_or_corrupt_files(self) -> None:
        self.assertIsNone(update_status.read_status(self.database))
        update_status.status_path(self.database).write_text("{not json", encoding="utf-8")
        self.assertIsNone(update_status.read_status(self.database))
        update_status.status_path(self.database).write_text(json.dumps(failing()), encoding="utf-8")
        self.assertEqual(update_status.read_status(self.database)["failures"], 3)

    def test_alert_is_due_once_per_failure_streak(self) -> None:
        status = failing()
        self.assertTrue(update_status.alert_due(status, self.database))
        update_status.mark_alerted(status, self.database)
        self.assertFalse(update_status.alert_due(failing(failures=9), self.database))
        self.assertTrue(update_status.alert_due(failing(since=1_800_000_000.0), self.database))
        self.assertFalse(update_status.alert_due({"result": "current"}, self.database))
        self.assertFalse(update_status.alert_due(None, self.database))

    def test_ready_marker_records_time_and_process(self) -> None:
        update_status.write_ready(self.database, now=123.0)
        marker = json.loads(self.database.with_name("bot-ready.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["ready_at"], 123.0)
        self.assertIsInstance(marker["pid"], int)


class UpdateAlertTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "tracker.sqlite3"
        self.owner = SimpleNamespace(send=mock.AsyncMock())
        self.bot = SimpleNamespace(
            config=SimpleNamespace(database_path=self.database, owner_user_id=31, timezone="UTC"),
            fetch_user=mock.AsyncMock(return_value=self.owner),
        )

    async def alert(self) -> None:
        await TrackerClient._alert_update_failure(self.bot)

    async def test_owner_gets_one_dm_per_failure_streak(self) -> None:
        await self.alert()
        self.owner.send.assert_not_called()
        update_status.status_path(self.database).write_text(json.dumps(failing()), encoding="utf-8")
        await self.alert()
        await self.alert()
        self.owner.send.assert_awaited_once()
        self.bot.fetch_user.assert_awaited_with(31)
        text = self.owner.send.await_args.args[0]
        self.assertIn("automatic updates are failing", text)
        self.assertIn("journalctl -u flock-cctv-update.service", text)
        self.assertEqual(
            self.owner.send.await_args.kwargs["allowed_mentions"].to_dict(),
            discord.AllowedMentions.none().to_dict(),
        )

    async def test_closed_dms_are_not_retried_but_other_errors_are(self) -> None:
        update_status.status_path(self.database).write_text(json.dumps(failing()), encoding="utf-8")
        response = SimpleNamespace(status=500, reason="error")
        self.owner.send.side_effect = discord.HTTPException(response, "server error")
        with self.assertRaises(discord.HTTPException):
            await self.alert()
        self.assertTrue(update_status.alert_due(failing(), self.database))

        forbidden = SimpleNamespace(status=403, reason="Forbidden")
        self.owner.send.side_effect = discord.Forbidden(forbidden, "Cannot send messages to this user")
        with self.assertLogs("flock_cctv.bot", level="WARNING"):
            await self.alert()
        self.assertFalse(update_status.alert_due(failing(), self.database))


if __name__ == "__main__":
    unittest.main()
