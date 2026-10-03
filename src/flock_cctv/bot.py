"""discord.py adapter for the tracker service."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import discord
from discord import app_commands

from .collectors import Tracker, eligible_voice_channel_id
from .config import Config
from .storage import Store
from .avatar import BOT_DESCRIPTION, BOT_USERNAME, avatar_digest, profile_avatar
from .error_log import ErrorLogBuffer
from .evil import evil_messages
from . import update_status

logger = logging.getLogger(__name__)
PROFILE_REFRESH_SECONDS = 60 * 60
UPDATE_WATCH_SECONDS = 5 * 60
MAINTENANCE_SECONDS = 24 * 60 * 60
MENTION_REPLY = "I am evil Leland, more gay than the original"


async def _log_failure(action: Callable[[], Awaitable[object]], failure: str) -> None:
    """Run ``action``, logging (not raising) an ordinary failure as ``failure``."""
    try:
        await action()
    except Exception:
        logger.exception(failure)


async def _every(seconds: float, action: Callable[[], Awaitable[object]], failure: str) -> None:
    """Run ``action`` now and then every ``seconds``; failures are logged, not fatal."""
    while True:
        await _log_failure(action, failure)
        await asyncio.sleep(seconds)


class _InstanceLock:
    """Hold an advisory lock so two bot processes cannot share one database."""

    def __init__(self, database_path: Path) -> None:
        lock_path = database_path.with_name(database_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = lock_path.open("a+")
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._file.close()
            raise RuntimeError(f"another tracker process already owns {database_path}") from exc

    def release(self) -> None:
        if not self._file.closed:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            self._file.close()


class TrackerClient(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.voice_states = True
        intents.presences = True
        # Only Evil Leland reposts read message text; without Leland mode the bot
        # does not request the privileged Message Content intent at all.
        intents.message_content = config.leland_user_id is not None
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.store: Store | None = None
        self.tracker: Tracker | None = None
        self._instance_lock: _InstanceLock | None = None
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._profile_task: asyncio.Task[None] | None = None
        self._update_watch_task: asyncio.Task[None] | None = None
        self._setup_complete = False
        self._closing = False
        # When this process started, for the admin health report.
        self.started_at = time.time()
        self.error_log = ErrorLogBuffer()
        self._close_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        if self._setup_complete:
            return
        self._instance_lock = _InstanceLock(self.config.database_path)
        logging.getLogger("flock_cctv").addHandler(self.error_log)
        try:
            self.store = Store(
                self.config.database_path,
                self.config.backup_dir,
                self.config.timezone,
            )
            await self.store.initialize(time.time(), self.config.guild_id)
            self.tracker = Tracker(self.config, self.store)

            # Import here so the adapter's configuration and storage tests can
            # remain independent of the command module's registration helpers.
            from .commands import register_commands

            register_commands(self)
            await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
            self._checkpoint_task = asyncio.create_task(
                self._checkpoint_loop(), name="flock-cctv-checkpoint"
            )
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name="flock-cctv-maintenance"
            )
            self._setup_complete = True
        except Exception:
            if self.tracker is not None:
                try:
                    await self.tracker.shutdown()
                except Exception:
                    logger.exception("Could not close the tracker after startup failed")
                self.tracker = None
                self.store = None
            elif self.store is not None:
                try:
                    await self.store.close()
                except Exception:
                    logger.exception("Could not close the store after startup failed")
                self.store = None
            if self._instance_lock is not None:
                self._instance_lock.release()
                self._instance_lock = None
            logging.getLogger("flock_cctv").removeHandler(self.error_log)
            raise

    async def _checkpoint_loop(self) -> None:
        # Unlike the other loops, the first checkpoint waits one interval.
        while True:
            await asyncio.sleep(self.config.checkpoint_seconds)
            if self.tracker is None:
                continue
            await _log_failure(self._checkpoint_once, "Checkpoint failed")
            await _log_failure(self._alert_outages, "Could not check for outages to report")
            await self.flush_error_log()

    async def flush_error_log(self) -> None:
        """Write buffered warnings and errors to the Store for /flock debug errors."""
        store = self.store
        entries = self.error_log.drain()
        if store is None or not entries:
            self.error_log.restore(entries)
            return
        try:
            await store.record_errors(entries)
        except asyncio.CancelledError:
            self.error_log.restore(entries)
            raise
        except Exception:
            # Logging here would only add to the buffer that could not be saved;
            # the journal already has every entry.
            self.error_log.restore(entries)

    async def _alert_outages(self) -> None:
        """DM the owner about each long outage once collection is back."""
        store = self.store
        if store is None or self._closing:
            return
        outages = await store.due_outage_alerts()
        if not outages:
            return
        from .commands import outage_alert_text

        try:
            owner = await self.fetch_user(self.config.owner_user_id)
            await owner.send(
                outage_alert_text(outages, self.config.timezone),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            # Closed DMs will not open by retrying; /flock debug uptime lists it.
            logger.warning("Could not DM the owner about an outage (DMs are closed)")

    async def _checkpoint_once(self) -> None:
        tracker = self.tracker
        if tracker is None or self._closing:
            return
        # Retry storage reconciliation after transient failures even when no
        # further Gateway ready event arrives. Do not revive a lost Gateway.
        if (
            tracker.connected
            and not tracker.collection_ready
            and self.is_ready()
            and self._configured_guild() is not None
        ):
            await tracker.guild_available(self.voice_snapshot)
        await tracker.checkpoint()

    async def _maintenance_loop(self) -> None:
        await _every(MAINTENANCE_SECONDS, self._maintain_once, "Daily maintenance failed")

    async def _maintain_once(self) -> None:
        store = self.store
        tracker = self.tracker
        if store is None:
            return
        try:
            await store.maintenance(time.time(), self.config.retention_days)
        except Exception as exc:
            if tracker is not None:
                tracker.report_error("maintenance", exc)
            raise
        if tracker is not None:
            tracker.report_recovered("maintenance")

    async def _profile_loop(self) -> None:
        await _every(
            PROFILE_REFRESH_SECONDS, self._sync_profile, "Could not refresh the bot profile"
        )

    async def _update_watch_loop(self) -> None:
        await _every(
            UPDATE_WATCH_SECONDS, self._alert_update_failure, "Could not check the updater status"
        )

    async def _alert_update_failure(self) -> None:
        """DM the owner once when automatic updates start failing."""
        database_path = self.config.database_path
        status = await asyncio.to_thread(update_status.read_status, database_path)
        if not update_status.alert_due(status, database_path):
            return
        try:
            owner = await self.fetch_user(self.config.owner_user_id)
            await owner.send(
                update_status.alert_text(status, self.config.timezone),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            # Closed DMs will not open by retrying; /flock about still shows it.
            logger.warning("Could not DM the owner about failing updates (DMs are closed)")
        await asyncio.to_thread(update_status.mark_alerted, status, database_path)
        logger.warning("Automatic updates are failing; the owner was notified")

    async def _sync_profile(self) -> None:
        """Keep the bot's name, avatar, and application description as bundled."""
        if self.user is None:
            return
        edits: dict[str, object] = {}
        if self.user.name != BOT_USERNAME:
            edits["username"] = BOT_USERNAME
        avatar = await asyncio.to_thread(profile_avatar)
        source_hash = avatar_digest(avatar)
        marker_path = self.config.database_path.with_name("avatar-source.json")
        marker = update_status.read_json(marker_path) or {}
        current_avatar = self.user.avatar
        current_key = current_avatar.key if current_avatar is not None else None
        if marker.get("source") != source_hash or marker.get("bot_avatar") != current_key:
            edits["avatar"] = avatar
        if edits:
            updated = await self.user.edit(**edits)
            if "avatar" in edits:
                avatar_key = updated.avatar.key if updated.avatar is not None else None
                update_status.write_json(
                    marker_path, {"source": source_hash, "bot_avatar": avatar_key}
                )
            logger.info("Updated the bot profile (%s)", ", ".join(sorted(edits)))
        application = await self.application_info()
        if application.description != BOT_DESCRIPTION:
            await application.edit(description=BOT_DESCRIPTION)
            logger.info("Updated the bot application description")

    def _configured_guild(self) -> discord.Guild | None:
        """The configured guild when it is cached and available, else None."""
        guild = self.get_guild(self.config.guild_id)
        if guild is None or getattr(guild, "unavailable", False):
            return None
        return guild

    def voice_snapshot(self) -> dict[int, int]:
        """Map each non-bot member in an eligible cached voice channel to that channel.

        Built from the cached guild's voice and stage channels with the same
        eligibility rules as live voice events
        (``collectors.eligible_voice_channel_id``): the AFK channel, channels
        outside the ``VOICE_CHANNEL_IDS`` allowlist, channels that do not
        belong to the configured guild, and bots are all excluded. An unknown
        or unavailable guild yields an empty snapshot.
        """
        guild = self._configured_guild()
        if guild is None:
            return {}
        snapshot: dict[int, int] = {}
        channels = [
            *getattr(guild, "voice_channels", ()),
            *getattr(guild, "stage_channels", ()),
        ]
        for channel in channels:
            channel_id = eligible_voice_channel_id(self.config, guild, channel)
            if channel_id is None:
                continue
            for member in getattr(channel, "members", ()):
                if getattr(member, "bot", False):
                    continue
                snapshot[int(member.id)] = channel_id
        return snapshot

    async def _reconcile_gateway_ready(self) -> None:
        tracker = self.tracker
        if tracker is None or self._closing:
            return
        was_connected = tracker.connected
        await tracker.gateway_ready()
        if self._configured_guild() is None:
            return
        if not was_connected or not tracker.guild_is_available or not tracker.collection_ready:
            await tracker.ready(self.voice_snapshot)

    async def on_ready(self) -> None:
        try:
            await self._reconcile_gateway_ready()
            if self._profile_task is None and not self._closing:
                self._profile_task = asyncio.create_task(
                    self._profile_loop(), name="flock-cctv-profile"
                )
        except Exception:
            logger.exception("Could not start collection after Gateway ready")
        if self._closing:
            return
        try:
            # The updater waits for this marker before accepting a new version.
            await asyncio.to_thread(update_status.write_ready, self.config.database_path)
        except OSError:
            logger.exception("Could not write the Discord ready marker for the updater")
        if self._update_watch_task is None:
            self._update_watch_task = asyncio.create_task(
                self._update_watch_loop(), name="flock-cctv-update-watch"
            )

    async def on_resumed(self) -> None:
        try:
            await self._reconcile_gateway_ready()
        except Exception:
            logger.exception("Could not reconcile collection after Gateway resume")

    async def on_disconnect(self) -> None:
        tracker = self.tracker
        if tracker is None or self._closing:
            return
        try:
            await tracker.disconnected()
        except Exception:
            logger.exception("Could not close collection after Gateway disconnect")

    async def _configured_guild_returned(self, guild: discord.Guild, failure: str) -> None:
        tracker = self.tracker
        if (
            guild.id != self.config.guild_id
            or self._closing
            or getattr(guild, "unavailable", False)
            or tracker is None
        ):
            return
        await _log_failure(lambda: tracker.guild_available(self.voice_snapshot), failure)

    async def _configured_guild_lost(self, guild: discord.Guild, failure: str) -> None:
        tracker = self.tracker
        if guild.id != self.config.guild_id or self._closing or tracker is None:
            return
        await _log_failure(tracker.guild_unavailable, failure)

    async def on_guild_available(self, guild: discord.Guild) -> None:
        await self._configured_guild_returned(
            guild, "Could not reconcile collection after guild recovery"
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        # The bot may be invited back after it was removed. A new join is also
        # a fresh observation boundary, so reconcile only current cached state.
        await self._configured_guild_returned(
            guild, "Could not reconcile collection after joining configured guild"
        )

    async def on_guild_unavailable(self, guild: discord.Guild) -> None:
        await self._configured_guild_lost(
            guild, "Could not stop collection after guild became unavailable"
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self._configured_guild_lost(
            guild, "Could not stop collection after bot left configured guild"
        )

    async def on_message(self, message: discord.Message) -> None:
        tracker = self.tracker
        inserted = False
        reaction_due = False
        ordinary = getattr(message, "type", discord.MessageType.default) == discord.MessageType.default
        leland_user_id = self.config.leland_user_id
        # Reactions and evil-mode reposts are legacy Leland features. Everyone
        # else who is tracked is only counted.
        from_leland = (
            leland_user_id is not None
            and getattr(getattr(message, "author", None), "id", None) == leland_user_id
        )
        if tracker is not None:
            try:
                if from_leland:
                    inserted, reaction_due = await tracker.message_with_reaction(
                        message, ordinary=ordinary
                    )
                else:
                    inserted = await tracker.message(message)
            except Exception:
                # Never log message objects or their contents.
                logger.exception("Message collection failed")
        if from_leland and inserted and ordinary:
            try:
                if reaction_due:
                    await message.add_reaction(random.choice(("😂", "👸")))
            except Exception:
                logger.exception("Message reaction failed")
            try:
                state = await self.store.state() if self.store is not None else {}
                if state.get("evil_mode", False):
                    for content in evil_messages(getattr(message, "content", "")):
                        await message.channel.send(
                            content,
                            allowed_mentions=discord.AllowedMentions.none(),
                            suppress_embeds=True,
                        )
            except Exception:
                logger.exception("Evil-mode message repost failed")
        await self._reply_to_mention(message)

    async def _reply_to_mention(self, message: discord.Message) -> None:
        if self.config.leland_user_id is None:
            return  # The fixed evil-Leland reply is a legacy feature.
        bot_user = self.user
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        if (
            bot_user is None
            or getattr(guild, "id", None) != self.config.guild_id
            or getattr(author, "bot", False)
            or not any(
                getattr(user, "id", None) == bot_user.id
                for user in getattr(message, "mentions", ())
            )
        ):
            return
        try:
            await message.channel.send(
                MENTION_REPLY,
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=True,
            )
        except Exception:
            logger.exception("Could not respond to a bot mention")

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        tracker = self.tracker
        if tracker is None:
            return
        try:
            await tracker.voice(member, before, after)
        except Exception:
            logger.exception("Voice collection failed")

    async def close(self) -> None:
        async with self._close_lock:
            if self._closing:
                return
            self._closing = True
            tasks = [
                task for task in (
                    self._checkpoint_task, self._maintenance_task,
                    self._profile_task, self._update_watch_task,
                )
                if task
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.flush_error_log()
            logging.getLogger("flock_cctv").removeHandler(self.error_log)
            try:
                if self.tracker is not None:
                    await self.tracker.shutdown()
                elif self.store is not None:
                    await self.store.close()
            except Exception:
                logger.exception("Graceful tracker shutdown failed")
            finally:
                if self._instance_lock is not None:
                    self._instance_lock.release()
                    self._instance_lock = None
                await super().close()


def create_bot(config: Config) -> TrackerClient:
    """Build a Discord client without starting network activity."""
    return TrackerClient(config)
