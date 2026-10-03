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


LELAND = 20


def make_config(
    root: Path,
    *,
    voice_channels: frozenset[int] | None = None,
    leland_user_id: int | None = LELAND,
) -> Config:
    return Config(
        token="test-token",
        guild_id=10,
        owner_user_id=99,
        output_channel_id=None,
        text_channel_ids=None,
        voice_channel_ids=voice_channels,
        timezone="UTC",
        database_path=root / "tracker.sqlite3",
        backup_dir=root / "backups",
        leland_user_id=leland_user_id,
    )


def voice_member(member_id: int, *, bot: bool = False):
    return SimpleNamespace(id=member_id, bot=bot)


def voice_channel(channel_id: int, *members, guild_id: int = 10):
    return SimpleNamespace(
        id=channel_id, guild=SimpleNamespace(id=guild_id), members=list(members)
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

    def test_message_content_intent_is_requested_only_for_leland_mode(self):
        with TemporaryDirectory() as directory:
            with_leland = create_bot(make_config(Path(directory), leland_user_id=20))
            without = create_bot(make_config(Path(directory), leland_user_id=None))
        self.assertTrue(with_leland.intents.message_content)
        self.assertFalse(without.intents.message_content)
        self.assertTrue(without.intents.presences)
        self.assertTrue(without.intents.guild_messages)

    def test_voice_snapshot_respects_allowlist_afk_guild_and_bot_exclusion(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory), voice_channels=frozenset({40, 42, 99})))
        guild = SimpleNamespace(
            id=10,
            unavailable=False,
            afk_channel=SimpleNamespace(id=99),
            voice_channels=[
                voice_channel(40, voice_member(20), voice_member(21), voice_member(22, bot=True)),
                voice_channel(41, voice_member(23)),  # Outside the allowlist.
                voice_channel(99, voice_member(24)),  # AFK channel.
                voice_channel(43, voice_member(27), guild_id=11),  # Wrong guild (and not allowed).
                voice_channel(42, voice_member(28), guild_id=11),  # Allowed ID, wrong guild.
            ],
            stage_channels=[voice_channel(42, voice_member(25), voice_member(26, bot=True))],
        )
        bot.get_guild = lambda guild_id: guild  # type: ignore[method-assign]
        self.assertEqual(bot.voice_snapshot(), {20: 40, 21: 40, 25: 42})

    def test_voice_snapshot_without_allowlist_includes_voice_and_stage_members(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        guild = SimpleNamespace(
            id=10, unavailable=False, afk_channel=None,
            voice_channels=[voice_channel(40, voice_member(20)), voice_channel(41, voice_member(21))],
            stage_channels=[voice_channel(50, voice_member(22))],
        )
        bot.get_guild = lambda guild_id: guild  # type: ignore[method-assign]
        self.assertEqual(bot.voice_snapshot(), {20: 40, 21: 41, 22: 50})
        guild.voice_channels = []
        guild.stage_channels = []
        self.assertEqual(bot.voice_snapshot(), {})

    def test_voice_snapshot_is_empty_for_unknown_or_unavailable_guild(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot.get_guild = lambda guild_id: None  # type: ignore[method-assign]
        self.assertEqual(bot.voice_snapshot(), {})
        guild = SimpleNamespace(
            id=10, unavailable=True, afk_channel=None,
            voice_channels=[voice_channel(40, voice_member(20))], stage_channels=[],
        )
        bot.get_guild = lambda guild_id: guild  # type: ignore[method-assign]
        self.assertEqual(bot.voice_snapshot(), {})

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


def message_tracker(*, inserted=False, reaction_due=False):
    return SimpleNamespace(
        message=AsyncMock(return_value=inserted),
        message_with_reaction=AsyncMock(return_value=(inserted, reaction_due)),
    )


class GracefulSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_mention_gets_exact_reply_without_ping(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot._connection.user = SimpleNamespace(id=55)
        bot.tracker = message_tracker()
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

    async def test_direct_mention_gets_no_reply_without_a_configured_leland(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory), leland_user_id=None))
        bot._connection.user = SimpleNamespace(id=55)
        bot.tracker = message_tracker()
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            author=SimpleNamespace(id=33, bot=False),
            mentions=[SimpleNamespace(id=55)],
            channel=channel,
        )
        await bot.on_message(message)
        channel.send.assert_not_awaited()

    async def test_evil_mode_reposts_new_leland_messages_without_mentions(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot.tracker = message_tracker(inserted=True)
        bot.store = SimpleNamespace(
            state=AsyncMock(return_value={"evil_mode": True}),
        )
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            content="Hello!", channel=channel, type=discord.MessageType.default,
            author=SimpleNamespace(id=LELAND, bot=False),
        )
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
        # Leland's messages never go through the plain per-person route.
        bot.tracker.message.assert_not_awaited()

    async def test_non_leland_tracked_messages_are_counted_without_reaction_or_repost(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot.tracker = message_tracker(inserted=True, reaction_due=True)
        bot.store = SimpleNamespace(state=AsyncMock(return_value={"evil_mode": True}))
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            content="Hello!", channel=channel, type=discord.MessageType.default,
            add_reaction=AsyncMock(),
            author=SimpleNamespace(id=33, bot=False),
        )
        await bot.on_message(message)
        bot.tracker.message.assert_awaited_once_with(message)
        bot.tracker.message_with_reaction.assert_not_awaited()
        message.add_reaction.assert_not_awaited()
        channel.send.assert_not_awaited()
        bot.store.state.assert_not_awaited()

    async def test_every_author_is_counted_normally_when_no_leland_is_configured(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory), leland_user_id=None))
        bot.tracker = message_tracker(inserted=True, reaction_due=True)
        bot.store = SimpleNamespace(state=AsyncMock(return_value={"evil_mode": True}))
        channel = SimpleNamespace(send=AsyncMock())
        message = SimpleNamespace(
            content="Hi", channel=channel, type=discord.MessageType.default,
            add_reaction=AsyncMock(), author=SimpleNamespace(id=20, bot=False),
        )
        await bot.on_message(message)
        bot.tracker.message.assert_awaited_once_with(message)
        bot.tracker.message_with_reaction.assert_not_awaited()
        message.add_reaction.assert_not_awaited()
        channel.send.assert_not_awaited()

    async def test_message_collection_failure_does_not_react_or_repost(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot.tracker = SimpleNamespace(
            message=AsyncMock(side_effect=OSError("x")),
            message_with_reaction=AsyncMock(side_effect=OSError("x")),
        )
        channel = SimpleNamespace(send=AsyncMock())
        for author_id in (LELAND, 33):
            message = SimpleNamespace(
                content="Hi", channel=channel, type=discord.MessageType.default,
                add_reaction=AsyncMock(), author=SimpleNamespace(id=author_id, bot=False),
            )
            with self.assertLogs("flock_cctv.bot", level="ERROR") as logs:
                await bot.on_message(message)
            message.add_reaction.assert_not_awaited()
            channel.send.assert_not_awaited()
            # Never log message contents.
            self.assertNotIn("Hi", "".join(logs.output))

    async def test_reaction_mode_reacts_only_when_due_on_new_ordinary_leland_messages(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        bot.tracker = SimpleNamespace(
            message_with_reaction=AsyncMock(side_effect=[(True, False), (True, True), (False, False), (True, False)]),
            message=AsyncMock(return_value=True),
        )
        bot.store = SimpleNamespace(
            state=AsyncMock(return_value={"evil_mode": False}),
        )
        message = SimpleNamespace(
            type=discord.MessageType.default,
            add_reaction=AsyncMock(),
            author=SimpleNamespace(id=LELAND, bot=False),
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
        bot.voice_snapshot = lambda: {20: 40, 21: 40}
        await bot._checkpoint_once()
        # The Tracker takes the snapshot itself, once it holds its lock.
        tracker.guild_available.assert_awaited_once_with(bot.voice_snapshot)
        tracker.checkpoint.assert_awaited_once()
        tracker.guild_available.reset_mock()
        tracker.connected = False
        await bot._checkpoint_once()
        tracker.guild_available.assert_not_awaited()

    async def test_gateway_and_guild_events_pass_the_voice_snapshot(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
        snapshot = {20: 40, 21: 40}
        bot.voice_snapshot = lambda: snapshot
        bot.get_guild = lambda guild_id: SimpleNamespace(unavailable=False)
        tracker = SimpleNamespace(
            connected=False, guild_is_available=False, collection_ready=False,
            gateway_ready=AsyncMock(), ready=AsyncMock(), guild_available=AsyncMock(),
        )
        bot.tracker = tracker
        await bot._reconcile_gateway_ready()
        tracker.gateway_ready.assert_awaited_once()
        tracker.ready.assert_awaited_once_with(bot.voice_snapshot)
        self.assertEqual(tracker.ready.await_args.args[0](), snapshot)

        guild = SimpleNamespace(id=10, unavailable=False)
        await bot.on_guild_available(guild)
        tracker.guild_available.assert_awaited_once_with(bot.voice_snapshot)
        tracker.guild_available.reset_mock()
        await bot.on_guild_join(guild)
        tracker.guild_available.assert_awaited_once_with(bot.voice_snapshot)
        tracker.guild_available.reset_mock()
        await bot.on_guild_available(SimpleNamespace(id=11, unavailable=False))
        tracker.guild_available.assert_not_awaited()

    async def _run_on_ready(self, bot):
        bot._reconcile_gateway_ready = AsyncMock()
        bot._avatar_loop = AsyncMock()
        bot._update_watch_loop = AsyncMock()
        with patch("flock_cctv.bot.update_status.write_ready"):
            await bot.on_ready()
        await asyncio.sleep(0)

    async def test_avatar_task_starts_only_when_leland_is_configured(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory)))
            await self._run_on_ready(bot)
            self.assertIsNotNone(bot._avatar_task)
            bot._avatar_loop.assert_awaited_once()

            without = create_bot(make_config(Path(directory), leland_user_id=None))
            await self._run_on_ready(without)
            self.assertIsNone(without._avatar_task)
            without._avatar_loop.assert_not_awaited()
            # The rest of start-up (update watch) still runs.
            self.assertIsNotNone(without._update_watch_task)

    async def test_sync_avatar_does_nothing_without_a_configured_leland(self):
        with TemporaryDirectory() as directory:
            bot = create_bot(make_config(Path(directory), leland_user_id=None))
        guild = SimpleNamespace(fetch_member=AsyncMock())
        bot.get_guild = lambda guild_id: guild
        bot._connection.user = SimpleNamespace(avatar=None, edit=AsyncMock())
        await bot._sync_avatar()
        guild.fetch_member.assert_not_awaited()
        bot.user.edit.assert_not_awaited()

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
