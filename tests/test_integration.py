"""Offline acceptance checks using the real adapter, collector, and database."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from flock_cctv.bot import create_bot
from flock_cctv.config import Config

GUILD_ID = 111111111111111111
OWNER = 333333333333333333
ANA = 222222222222222221  # Tracked; also the configured Leland in some tests.
BEN = 222222222222222222  # Tracked.
CAL = 222222222222222223  # Never tracked.
TEXT_CHANNEL = 111111111111111112
VOICE_CHANNEL = 111111111111111113


def make_config(root: Path, *, leland_user_id: int | None = None) -> Config:
    return Config(
        token="offline-test-token",
        guild_id=GUILD_ID,
        owner_user_id=OWNER,
        output_channel_id=None,
        text_channel_ids=None,
        voice_channel_ids=None,
        timezone="America/Costa_Rica",
        database_path=root / "tracker.sqlite3",
        backup_dir=root / "backups",
        leland_user_id=leland_user_id,
    )


def roster(path: Path, user_id: int) -> list[int] | None:
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT member_ids FROM voice_company_current WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    return None if row is None else [int(value) for value in json.loads(row[0])]


def fake_guild(*member_ids: int):
    guild = SimpleNamespace(id=GUILD_ID, unavailable=False, afk_channel=None)
    channel = SimpleNamespace(
        id=VOICE_CHANNEL,
        guild=guild,
        members=[SimpleNamespace(id=member_id, bot=False) for member_id in member_ids],
    )
    guild.voice_channels = [channel]
    guild.stage_channels = []
    return guild, channel


def chat_message(message_id: int, author_id: int, *, channel=None, created=None):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=GUILD_ID),
        author=SimpleNamespace(id=author_id, bot=False),
        channel=channel or SimpleNamespace(id=TEXT_CHANNEL, send=AsyncMock()),
        created_at=created or datetime.now(timezone.utc),
        content="Hello!",
        type=discord.MessageType.default,
        add_reaction=AsyncMock(),
        mentions=[],
    )


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_composition_registers_the_flock_command_group(self):
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            bot = create_bot(config)
            async with bot:
                with patch.object(bot.tree, "sync", new=AsyncMock()) as sync:
                    await bot.setup_hook()
                sync.assert_awaited_once_with(guild=discord.Object(id=GUILD_ID))
                groups = bot.tree.get_commands(guild=discord.Object(id=GUILD_ID))
                self.assertEqual({group.name for group in groups}, {"flock"})
                self.assertTrue(bot.intents.message_content)
                self.assertTrue(bot.intents.presences)
                self.assertTrue(bot.intents.guild_messages)
                self.assertTrue(bot.intents.voice_states)

    async def test_collection_and_persistence_for_two_tracked_people_without_discord(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root, leland_user_id=ANA)
            bot = create_bot(config)
            async with bot:
                # Command registration is covered separately; keep this flow
                # independent of the command module.
                with patch("flock_cctv.commands.register_commands"), patch.object(
                    bot.tree, "sync", new=AsyncMock()
                ):
                    await bot.setup_hook()
                # Nobody is tracked by default.
                self.assertEqual(await bot.store.active_user_ids(), frozenset())

                guild, channel = fake_guild(ANA, BEN, CAL)
                bot.get_guild = lambda guild_id: guild  # type: ignore[method-assign]
                self.assertEqual(
                    bot.voice_snapshot(),
                    {ANA: VOICE_CHANNEL, BEN: VOICE_CHANNEL, CAL: VOICE_CHANNEL},
                )

                await bot.tracker.ready({})
                self.assertTrue(await bot.tracker.track_user(ANA, OWNER, {}))
                self.assertTrue(await bot.tracker.track_user(BEN, OWNER, {}))
                self.assertFalse(await bot.tracker.track_user(BEN, OWNER, {}))
                self.assertEqual(bot.tracker.tracked_ids, frozenset({ANA, BEN}))
                # Reconciling with the live snapshot starts incomplete visits for both.
                await bot.tracker.guild_unavailable()
                await bot.tracker.guild_available(bot.voice_snapshot())
                self.assertEqual(roster(config.database_path, ANA), [BEN, CAL])
                self.assertEqual(roster(config.database_path, BEN), [ANA, CAL])
                self.assertIsNone(roster(config.database_path, CAL))

                # Voice events update every tracked roster.
                cal = SimpleNamespace(id=CAL, guild=guild, bot=False)
                await bot.on_voice_state_update(
                    cal, SimpleNamespace(channel=channel), SimpleNamespace(channel=None)
                )
                self.assertEqual(roster(config.database_path, ANA), [BEN])
                self.assertEqual(roster(config.database_path, BEN), [ANA])
                ben = SimpleNamespace(id=BEN, guild=guild, bot=False)
                await bot.on_voice_state_update(
                    ben, SimpleNamespace(channel=channel), SimpleNamespace(channel=None)
                )
                self.assertEqual(roster(config.database_path, ANA), [])
                self.assertIsNone(roster(config.database_path, BEN))

                # Messages: Leland-only reactions and reposts, everyone tracked is counted.
                await bot.store.set_evil_mode(True)
                with patch("flock_cctv.storage.random.randint", return_value=1):
                    await bot.store.set_reaction_mode(True)  # Next ordinary Leland message is due.
                ana_channel = SimpleNamespace(id=TEXT_CHANNEL, send=AsyncMock())
                ben_channel = SimpleNamespace(id=TEXT_CHANNEL, send=AsyncMock())
                cal_channel = SimpleNamespace(id=TEXT_CHANNEL, send=AsyncMock())
                base = 1440413172649164813
                from_ana = chat_message(base, ANA, channel=ana_channel)
                from_ben = chat_message(base + 1, BEN, channel=ben_channel)
                from_cal = chat_message(base + 2, CAL, channel=cal_channel)
                await bot.on_message(from_ana)
                await bot.on_message(from_ana)  # Duplicate delivery.
                await bot.on_message(from_ben)
                await bot.on_message(from_cal)
                now = time.time()
                self.assertEqual((await bot.store.stats(ANA, "all", now))["messages"], 1)
                self.assertEqual((await bot.store.stats(BEN, "all", now))["messages"], 1)
                self.assertEqual((await bot.store.stats(CAL, "all", now))["messages"], 0)
                from_ana.add_reaction.assert_awaited_once()
                ana_channel.send.assert_awaited_once()
                self.assertFalse(ana_channel.send.await_args.kwargs["allowed_mentions"].users)
                from_ben.add_reaction.assert_not_awaited()
                ben_channel.send.assert_not_awaited()
                from_cal.add_reaction.assert_not_awaited()
                cal_channel.send.assert_not_awaited()

                await bot.tracker.pause(OWNER)
                paused_message = chat_message(base + 3, BEN)
                await bot.on_message(paused_message)
                self.assertEqual((await bot.store.stats(BEN, "all", time.time()))["messages"], 1)

            restarted = create_bot(config)
            async with restarted:
                with patch("flock_cctv.commands.register_commands"), patch.object(
                    restarted.tree, "sync", new=AsyncMock()
                ):
                    await restarted.setup_hook()
                state = await restarted.store.state()
                self.assertTrue(state["paused"])
                self.assertEqual(str(state["paused_by"]), str(OWNER))
                self.assertEqual(await restarted.store.active_user_ids(), frozenset({ANA, BEN}))
                for person in (ANA, BEN):
                    stats = await restarted.store.stats(person, "all", time.time())
                    self.assertEqual(stats["messages"], 1)
                    self.assertTrue(stats["tracked"])

    async def test_without_leland_everyone_tracked_is_plain_collection(self):
        with TemporaryDirectory() as directory:
            config = make_config(Path(directory))
            bot = create_bot(config)
            async with bot:
                with patch("flock_cctv.commands.register_commands"), patch.object(
                    bot.tree, "sync", new=AsyncMock()
                ):
                    await bot.setup_hook()
                await bot.tracker.ready({})
                await bot.tracker.track_user(ANA, OWNER, {})
                await bot.store.set_evil_mode(True)  # Ignored: no Leland configured.
                channel = SimpleNamespace(id=TEXT_CHANNEL, send=AsyncMock())
                message = chat_message(1, ANA, channel=channel)
                await bot.on_message(message)
                self.assertEqual((await bot.store.stats(ANA, "all", time.time()))["messages"], 1)
                message.add_reaction.assert_not_awaited()
                channel.send.assert_not_awaited()
                self.assertIsNone(bot._avatar_task)


if __name__ == "__main__":
    unittest.main()
