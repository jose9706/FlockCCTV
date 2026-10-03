from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from PIL import Image

from flock_cctv.avatar import BOT_DESCRIPTION, BOT_USERNAME, profile_avatar
from flock_cctv.bot import create_bot
from flock_cctv.config import Config


class ProfileAvatarTests(unittest.TestCase):
    def test_bundled_avatar_is_a_square_image(self):
        with Image.open(BytesIO(profile_avatar())) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size[0], image.size[1])

    def test_description_fits_discord_limit(self):
        self.assertLessEqual(len(BOT_DESCRIPTION), 400)


class ProfileSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_applies_the_profile_once(self):
        with TemporaryDirectory() as directory:
            config = Config(
                token="test-token", guild_id=10, owner_user_id=99,
                output_channel_id=None, text_channel_ids=None, voice_channel_ids=None,
                timezone="UTC", database_path=Path(directory) / "tracker.sqlite3",
                backup_dir=Path(directory) / "backups", leland_user_id=None,
            )
            bot = create_bot(config)
            updated = SimpleNamespace(avatar=SimpleNamespace(key="flock"))
            user = SimpleNamespace(
                name="Leland Tracker",
                avatar=SimpleNamespace(key="inverted-leland"),
                edit=AsyncMock(return_value=updated),
            )
            bot._connection.user = user
            application = SimpleNamespace(description="", edit=AsyncMock())
            bot.application_info = AsyncMock(return_value=application)

            await bot._sync_profile()
            user.edit.assert_awaited_once()
            kwargs = user.edit.await_args.kwargs
            self.assertEqual(kwargs["username"], BOT_USERNAME)
            self.assertEqual(kwargs["avatar"], profile_avatar())
            application.edit.assert_awaited_once_with(description=BOT_DESCRIPTION)
            self.assertTrue(config.database_path.with_name("avatar-source.json").exists())

            # Once applied, nothing is edited again.
            user.name = BOT_USERNAME
            user.avatar = updated.avatar
            application.description = BOT_DESCRIPTION
            await bot._sync_profile()
            user.edit.assert_awaited_once()
            application.edit.assert_awaited_once()

            # A manual avatar change is reverted on the next check.
            user.avatar = SimpleNamespace(key="manual")
            await bot._sync_profile()
            self.assertEqual(user.edit.await_count, 2)
            self.assertNotIn("username", user.edit.await_args.kwargs)
