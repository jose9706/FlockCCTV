from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from flock_cctv.__main__ import _run_bot
from flock_cctv.bot import MENTION_REPLY, _InstanceLock, create_bot
from flock_cctv.config import Config


def make_config(root: Path, *, voice_channels: frozenset[int] | None = None) -> Config:
    return Config(
        token="test-token",
        guild_id=10,
        target_user_id=20,
        owner_user_id=99,
        output_channel_id=None,
        text_channel_ids=None,
        voice_channel_ids=voice_channels,
        timezone="UTC",
        database_path=root / "tracker.sqlite3",
        backup_dir=root / "backups",
    )


class BotTests(unittest.TestCase):
    def test_client_uses_only_required_gateway_intents_and_blocks_mentions(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        self.assertTrue(bot.intents.guilds)
        self.assertTrue(bot.intents.guild_messages)
        self.assertTrue(bot.intents.voice_states)
        self.assertTrue(bot.intents.message_content)
        self.assertTrue(bot.intents.presences)
        self.assertFalse(bot.allowed_mentions.everyone)
        self.assertFalse(bot.allowed_mentions.users)
        self.assertFalse(bot.allowed_mentions.roles)
        self.assertFalse(bot.allowed_mentions.replied_user)

    def test_current_voice_channel_respects_allowlist_and_afk_exclusion(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory), voice_channels=frozenset({40})))
        guild = SimpleNamespace(
            id=10,
            unavailable=False,
            afk_channel=SimpleNamespace(id=99),
            get_member=lambda user_id: SimpleNamespace(
                voice=SimpleNamespace(
                    channel=SimpleNamespace(id=40, guild=SimpleNamespace(id=10))
                )
            ),
        )
        bot.get_guild = lambda guild_id: guild  # type: ignore[method-assign]
        self.assertEqual(bot.current_voice_channel_id(), 40)
        guild.get_member = lambda user_id: SimpleNamespace(
            voice=SimpleNamespace(channel=SimpleNamespace(
                id=40, guild=SimpleNamespace(id=10),
                members=[
                    SimpleNamespace(id=20, bot=False),
                    SimpleNamespace(id=21, bot=False),
                    SimpleNamespace(id=22, bot=True),
                ],
            ))
        )
        self.assertEqual(bot.current_voice_companions(), frozenset({21}))
        guild.get_member = lambda user_id: SimpleNamespace(
            voice=SimpleNamespace(
                channel=SimpleNamespace(id=41, guild=SimpleNamespace(id=10))
            )
        )
        self.assertIsNone(bot.current_voice_channel_id())
        guild.get_member = lambda user_id: SimpleNamespace(
            voice=SimpleNamespace(
                channel=SimpleNamespace(id=99, guild=SimpleNamespace(id=10))
            )
        )
        self.assertIsNone(bot.current_voice_channel_id())

    def test_database_instance_lock_is_exclusive_and_released(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "tracker.sqlite3"
            first = _InstanceLock(database)
            try:
                with self.assertRaises(RuntimeError):
                    _InstanceLock(database)
            finally:
                first.release()
            second = _InstanceLock(database)
            second.release()


class GracefulSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_mention_gets_exact_reply_without_ping(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot._connection.user = SimpleNamespace(id=55)
        bot.tracker = SimpleNamespace(message_with_reaction=AsyncMock(return_value=(False, False)))
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            author=SimpleNamespace(id=33, bot=False),
            mentions=[SimpleNamespace(id=55)],
            channel=channel,
        )
        await bot.on_message(message)
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.args[0], MENTION_REPLY)
        self.assertFalse(channel.send.await_args.kwargs["allowed_mentions"].users)
        self.assertTrue(channel.send.await_args.kwargs["suppress_embeds"])

        channel.send.reset_mock()
        message.mentions = [SimpleNamespace(id=99)]
        await bot.on_message(message)
        channel.send.assert_not_awaited()
        message.mentions = [SimpleNamespace(id=55)]
        message.author.bot = True
        await bot.on_message(message)
        channel.send.assert_not_awaited()
        message.author.bot = False
        message.guild.id = 11
        await bot.on_message(message)
        channel.send.assert_not_awaited()

    async def test_evil_mode_reposts_new_target_messages_without_mentions(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot.tracker = SimpleNamespace(message_with_reaction=AsyncMock(return_value=(True, False)))
        bot.store = SimpleNamespace(
            state=AsyncMock(return_value={"evil_mode": True}),
        )
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(content="Hello!", channel=channel, type=discord.MessageType.default)
        await bot.on_message(message)
        channel.send.assert_awaited_once()
        self.assertIn("¡", channel.send.await_args.args[0])
        self.assertFalse(channel.send.await_args.kwargs["allowed_mentions"].everyone)
        self.assertTrue(channel.send.await_args.kwargs["suppress_embeds"])
        bot.tracker.message_with_reaction.return_value = (False, False)
        await bot.on_message(message)
        channel.send.assert_awaited_once()
        bot.tracker.message_with_reaction.return_value = (True, False)
        bot.store.state.return_value = {"evil_mode": False}
        await bot.on_message(message)
        channel.send.assert_awaited_once()

    async def test_reaction_mode_reacts_only_when_due_on_new_ordinary_messages(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot.tracker = SimpleNamespace(
            message_with_reaction=AsyncMock(side_effect=[(True, False), (True, True), (False, False), (True, False)])
        )
        bot.store = SimpleNamespace(
            state=AsyncMock(return_value={"evil_mode": False}),
        )
        message = SimpleNamespace(
            type=discord.MessageType.default,
            add_reaction=AsyncMock(),
        )
        await bot.on_message(message)
        message.add_reaction.assert_not_awaited()
        with patch("flock_cctv.bot.random.choice", return_value="👸"):
            await bot.on_message(message)
        message.add_reaction.assert_awaited_once_with("👸")
        await bot.on_message(message)
        message.type = discord.MessageType.pins_add
        await bot.on_message(message)
        self.assertEqual(bot.tracker.message_with_reaction.await_count, 4)
        self.assertFalse(bot.tracker.message_with_reaction.await_args.kwargs["ordinary"])
        message.add_reaction.assert_awaited_once()

    async def test_checkpoint_retries_storage_reconciliation_only_with_live_gateway(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        tracker = SimpleNamespace(
            connected=True, collection_ready=False,
            guild_available=AsyncMock(), checkpoint=AsyncMock(),
        )
        bot.tracker = tracker
        bot.is_ready = lambda: True
        bot.get_guild = lambda guild_id: SimpleNamespace(unavailable=False)
        bot.current_voice_channel_id = lambda: 40
        bot.current_voice_companions = lambda: frozenset({21})
        await bot._checkpoint_once()
        tracker.guild_available.assert_awaited_once_with(40, companions=frozenset({21}))
        tracker.checkpoint.assert_awaited_once()
        tracker.guild_available.reset_mock()
        tracker.connected = False
        await bot._checkpoint_once()
        tracker.guild_available.assert_not_awaited()

    async def test_sigterm_path_closes_client_and_waits_for_gateway(self):
        stopping = asyncio.Event()

        class FakeBot:
            def __init__(self):
                self.closed = False
                self.closed_event = asyncio.Event()
                self.token = None
                self.close_count = 0

            async def start(self, token):
                self.token = token
                stopping.set()
                await self.closed_event.wait()

            async def close(self):
                self.close_count += 1
                self.closed = True
                self.closed_event.set()

            def is_closed(self):
                return self.closed

        bot = FakeBot()
        await _run_bot(bot, "secret-token", stop_event=stopping)
        self.assertEqual(bot.token, "secret-token")
        self.assertTrue(bot.closed)
        self.assertEqual(bot.close_count, 1)

    async def test_daily_maintenance_runs_immediately_then_sleeps(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        store = SimpleNamespace(maintenance=AsyncMock())
        tracker = SimpleNamespace(
            report_error=Mock(),
            report_recovered=Mock(),
        )
        bot.store = store
        bot.tracker = tracker
        with patch("flock_cctv.bot.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await bot._maintenance_loop()
        store.maintenance.assert_awaited_once()
        tracker.report_recovered.assert_called_once_with("maintenance")


if __name__ == "__main__":
    unittest.main()
