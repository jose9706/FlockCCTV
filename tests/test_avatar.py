from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from PIL import Image

from flock_cctv.avatar import invert_avatar
from flock_cctv.bot import create_bot
from flock_cctv.config import Config


def sample_image() -> bytes:
    image = Image.new("RGBA", (256, 256), (20, 100, 230, 80))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class AvatarImageTests(unittest.TestCase):
    def test_inverts_colours_and_keeps_alpha(self):
        result = invert_avatar(sample_image())
        with Image.open(BytesIO(result)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (256, 256))
            self.assertEqual(image.convert("RGBA").getpixel((0, 0)), (235, 155, 25, 80))


class AvatarSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_edits_once_for_each_source_avatar(self):
        with TemporaryDirectory() as directory:
            config = Config(
                token="test-token", guild_id=10, owner_user_id=99,
                output_channel_id=None, text_channel_ids=None, voice_channel_ids=None,
                timezone="UTC", database_path=Path(directory) / "tracker.sqlite3",
                backup_dir=Path(directory) / "backups", leland_user_id=20,
            )
            bot = create_bot(config)
            class FakeAsset:
                def __init__(self):
                    self.url = "https://cdn.discordapp.com/avatar-one.png"
                    self.read = AsyncMock(return_value=sample_image())

                def with_size(self, size):
                    return self

                def __str__(self):
                    return self.url

            asset = FakeAsset()
            member = SimpleNamespace(display_avatar=asset)
            guild = SimpleNamespace(fetch_member=AsyncMock(return_value=member))
            bot.get_guild = lambda guild_id: guild
            current = SimpleNamespace(key="original")
            updated = SimpleNamespace(avatar=SimpleNamespace(key="inverted"))
            user = SimpleNamespace(avatar=current, edit=AsyncMock(return_value=updated))
            bot._connection.user = user

            await bot._sync_avatar()
            # The avatar mirrors the configured Leland user's server avatar.
            guild.fetch_member.assert_awaited_once_with(20)
            user.edit.assert_awaited_once()
            self.assertTrue(user.edit.await_args.kwargs["avatar"].startswith(b"\x89PNG"))
            self.assertTrue(config.database_path.with_name("avatar-source.json").exists())

            user.avatar = updated.avatar
            await bot._sync_avatar()
            user.edit.assert_awaited_once()

            asset.url = "https://cdn.discordapp.com/avatar-two.png"
            await bot._sync_avatar()
            self.assertEqual(user.edit.await_count, 2)
