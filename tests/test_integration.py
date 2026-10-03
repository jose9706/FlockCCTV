"""Offline acceptance checks using the real adapter, collector, and database."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from flock_cctv.bot import create_bot
from flock_cctv.config import Config


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_composition_collection_and_persistence_without_discord(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config(
                token="offline-test-token",
                guild_id=111111111111111111,
                target_user_id=222222222222222221,
                owner_user_id=333333333333333333,
                output_channel_id=None,
                text_channel_ids=None,
                voice_channel_ids=None,
                timezone="America/Costa_Rica",
                database_path=root / "tracker.sqlite3",
                backup_dir=root / "backups",
            )
            bot = create_bot(config)
            async with bot:
                with patch.object(bot.tree, "sync", new=AsyncMock()) as sync:
                    await bot.setup_hook()
                sync.assert_awaited_once_with(guild=discord.Object(id=config.guild_id))
                groups = bot.tree.get_commands(guild=discord.Object(id=config.guild_id))
                self.assertEqual({group.name for group in groups}, {"leland"})
                self.assertTrue(bot.intents.message_content)
                self.assertTrue(bot.intents.presences)
                self.assertTrue(bot.intents.guild_messages)
                self.assertTrue(bot.intents.voice_states)

                await bot.tracker.ready(None)
                now = time.time()
                message = SimpleNamespace(
                    id=1440413172649164813,
                    guild=SimpleNamespace(id=config.guild_id),
                    author=SimpleNamespace(id=config.target_user_id, bot=False),
                    channel=SimpleNamespace(id=111111111111111112),
                    created_at=datetime.fromtimestamp(now, timezone.utc),
                )
                await bot.tracker.message(message)
                await bot.tracker.message(message)
                other = SimpleNamespace(**vars(message))
                other.id += 1
                other.author = SimpleNamespace(id=12, bot=False)
                await bot.tracker.message(other)
                self.assertEqual((await bot.store.stats("all", time.time()))["messages"], 1)

                await bot.tracker.pause(config.owner_user_id)
                paused_message = SimpleNamespace(**vars(message))
                paused_message.id += 2
                paused_message.created_at = datetime.now(timezone.utc)
                await bot.tracker.message(paused_message)
                self.assertEqual((await bot.store.stats("all", time.time()))["messages"], 1)

            restarted = create_bot(config)
            async with restarted:
                with patch.object(restarted.tree, "sync", new=AsyncMock()):
                    await restarted.setup_hook()
                state = await restarted.store.state()
                self.assertTrue(state["paused"])
                self.assertEqual(str(state["paused_by"]), str(config.owner_user_id))
                self.assertEqual((await restarted.store.stats("all", time.time()))["messages"], 1)


if __name__ == "__main__":
    unittest.main()
