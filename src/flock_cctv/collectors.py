"""Filtered Discord event collection with conservative connection recovery."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .storage import Store


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        # Discord supplies aware UTC datetimes. Treat a naive test value as UTC
        # instead of letting the host's local timezone affect stored timestamps.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    converter = getattr(value, "timestamp", None)
    if callable(converter):
        try:
            return float(converter())
        except (TypeError, ValueError, OSError):
            return None
    return None


def _id(obj: Any) -> int | None:
    value = getattr(obj, "id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class Tracker:
    """Serializes collector state and forwards eligible metadata to ``Store``."""

    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.connected = False  # Gateway health
        self.guild_is_available = False
        self.collection_ready = False
        self.last_error: str | None = None
        self._error_operation: str | None = None
        self._lock = asyncio.Lock()
        self._collection_since: float | None = None
        self._shutdown = False

    def _record_error(self, operation: str, exc: BaseException) -> None:
        # Error text from a dependency can include implementation details. Keep
        # status concise and safe; the adapter logs the traceback separately.
        self.last_error = f"{operation} failed ({type(exc).__name__})"
        self._error_operation = operation

    def report_error(self, operation: str, exc: BaseException) -> None:
        """Publish an adapter-owned failure, such as a maintenance error."""
        self._record_error(operation, exc)

    def report_recovered(self, operation: str) -> None:
        """Clear status only when the operation that failed has recovered."""
        if self._error_operation == operation:
            self.last_error = None
            self._error_operation = None

    def _recovered(self, operation: str) -> None:
        self.report_recovered(operation)

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    def _eligible_voice_channel(self, channel_id: int | None) -> int | None:
        if channel_id is None:
            return None
        allowlist = self.config.voice_channel_ids
        return channel_id if allowlist is None or channel_id in allowlist else None

    async def _start_guild_collection(
        self, voice_channel_id: int | None, now: float, companions: frozenset[int]
    ) -> None:
        self.collection_ready = False
        state = await self.store.state()
        self._collection_since = now
        if state["paused"]:
            self.collection_ready = True
            return
        # A prior disconnect callback may have failed after Gateway state was
        # cleared. Retry closure before connect so an old open segment cannot
        # span the outage when Store.connect sees it as already open.
        await self.store.disconnect(now)
        await self.store.connect(now)
        channel_id = self._eligible_voice_channel(voice_channel_id)
        if channel_id is not None:
            await self.store.voice_transition(
                channel_id, now, complete_start=False, companions=companions
            )
        self.collection_ready = True
        self._recovered("disconnect")
        self._recovered("guild unavailable")
        self._recovered("guild recovery")
        self._recovered("ready")

    async def gateway_ready(self) -> None:
        """Record Gateway health before the configured guild becomes available."""
        async with self._lock:
            self.connected = True
            self._shutdown = False

    async def ready(
        self, voice_channel_id: int | None, now: float | None = None,
        *, companions: frozenset[int] = frozenset(),
    ) -> None:
        """Handle the initial ready event or a fresh session for the target guild."""
        current = self._now(now)
        async with self._lock:
            try:
                self.connected = True
                self._shutdown = False
                self.guild_is_available = True
                self.collection_ready = False
                await self._start_guild_collection(voice_channel_id, current, companions)
                self._recovered("ready")
                self._recovered("guild recovery")
            except Exception as exc:
                self._record_error("ready", exc)
                raise

    async def guild_available(
        self, voice_channel_id: int | None, now: float | None = None,
        *, companions: frozenset[int] = frozenset(),
    ) -> None:
        """Reconcile cached voice state when the configured guild returns."""
        current = self._now(now)
        async with self._lock:
            try:
                was_available = self.guild_is_available
                self.guild_is_available = True
                if self.connected and (not was_available or not self.collection_ready):
                    await self._start_guild_collection(voice_channel_id, current, companions)
                self._recovered("guild recovery")
            except Exception as exc:
                self._record_error("guild recovery", exc)
                raise

    async def guild_unavailable(self, now: float | None = None) -> None:
        """Stop collection while the configured guild is unavailable or removed."""
        current = self._now(now)
        async with self._lock:
            was_available = self.guild_is_available
            self.guild_is_available = False
            self.collection_ready = False
            self._collection_since = None
            if not was_available:
                return
            try:
                # Store.disconnect closes voice at the last reliable checkpoint
                # and records the uncertain interval as a coverage gap.
                await self.store.disconnect(current)
                self._recovered("guild unavailable")
            except Exception as exc:
                self._record_error("guild unavailable", exc)
                raise

    async def disconnected(self, now: float | None = None) -> None:
        """Handle Gateway loss. Voice and messages stay disabled until ready."""
        current = self._now(now)
        async with self._lock:
            was_collecting = self.connected and self.guild_is_available
            self.connected = False
            self.guild_is_available = False
            self.collection_ready = False
            self._collection_since = None
            if not was_collecting:
                return
            try:
                await self.store.disconnect(current)
                self._recovered("disconnect")
            except Exception as exc:
                self._record_error("disconnect", exc)
                raise

    async def message(self, message: Any) -> bool:
        """Count one eligible message; return whether it was newly inserted."""
        inserted, _ = await self._message(message, reaction_eligible=False, ordinary=False)
        return inserted

    async def message_with_reaction(
        self, message: Any, *, ordinary: bool
    ) -> tuple[bool, bool]:
        """Count a message and return whether a reaction is due for it."""
        return await self._message(message, reaction_eligible=True, ordinary=ordinary)

    async def _message(
        self, message: Any, *, reaction_eligible: bool, ordinary: bool
    ) -> tuple[bool, bool]:
        async with self._lock:
            if (
                self._shutdown
                or not self.connected
                or not self.guild_is_available
                or not self.collection_ready
            ):
                return False, False
            guild_id = _id(getattr(message, "guild", None))
            author = getattr(message, "author", None)
            channel = getattr(message, "channel", None)
            message_id = _id(message)
            channel_id = _id(channel)
            created_at = _timestamp(getattr(message, "created_at", None))
            if (
                guild_id != self.config.guild_id
                or _id(author) != self.config.target_user_id
                or message_id is None
                or channel_id is None
                or created_at is None
                or bool(getattr(author, "bot", False))
            ):
                return False, False
            allowed = self.config.text_channel_ids
            if allowed is not None and channel_id not in allowed:
                return False, False
            try:
                state = await self.store.state()
                boundary = max(
                    float(state["tracking_since"]),
                    self._collection_since if self._collection_since is not None else float("inf"),
                )
                if state["paused"] or created_at < boundary:
                    return False, False
                if reaction_eligible:
                    result = await self.store.add_message_with_reaction(
                        message_id, channel_id, created_at, ordinary=ordinary
                    )
                else:
                    result = await self.store.add_message(message_id, channel_id, created_at), False
                self._recovered("message collection")
                return result
            except Exception as exc:
                self._record_error("message collection", exc)
                raise

    def _channel_id_for_state(self, member: Any, state: Any) -> int | None:
        channel = getattr(state, "channel", None)
        channel_id = _id(channel)
        if channel_id is None:
            return None
        guild = getattr(member, "guild", None)
        guild_id = _id(guild)
        channel_guild_id = _id(getattr(channel, "guild", None))
        if guild_id != self.config.guild_id:
            return None
        if channel_guild_id is not None and channel_guild_id != self.config.guild_id:
            return None
        afk_id = _id(getattr(guild, "afk_channel", None))
        if afk_id == channel_id:
            return None
        return self._eligible_voice_channel(channel_id)

    def _companions(self, channel: Any) -> frozenset[int]:
        return frozenset(
            member_id for peer in getattr(channel, "members", ())
            if not bool(getattr(peer, "bot", False))
            if (member_id := _id(peer)) is not None
            if member_id != self.config.target_user_id
        )

    async def voice(self, member: Any, before: Any, after: Any) -> None:
        """Process target channel changes and peers entering or leaving it."""
        async with self._lock:
            if (
                self._shutdown
                or not self.connected
                or not self.guild_is_available
                or not self.collection_ready
            ):
                return
            guild_id = _id(getattr(member, "guild", None))
            member_id = _id(member)
            if guild_id != self.config.guild_id or member_id is None:
                return
            previous = self._channel_id_for_state(member, before)
            current_channel = self._channel_id_for_state(member, after)
            if previous == current_channel:
                return
            if member_id != self.config.target_user_id and bool(getattr(member, "bot", False)):
                return
            try:
                state = await self.store.state()
                if state["paused"]:
                    return
                now = time.time()
                if member_id == self.config.target_user_id:
                    companions = self._companions(getattr(after, "channel", None))
                    await self.store.voice_transition(
                        current_channel, now, companions=companions
                    )
                else:
                    if previous is not None:
                        await self.store.companion_transition(previous, member_id, False, now)
                    if current_channel is not None:
                        await self.store.companion_transition(current_channel, member_id, True, now)
                self._recovered("voice collection")
            except Exception as exc:
                self._record_error("voice collection", exc)
                raise

    async def checkpoint(self) -> None:
        """Persist the current reliable boundary when the guild is collectible."""
        async with self._lock:
            if (
                self._shutdown
                or not self.connected
                or not self.guild_is_available
                or not self.collection_ready
            ):
                return
            try:
                await self.store.checkpoint(time.time())
                self._recovered("checkpoint")
            except Exception as exc:
                self._record_error("checkpoint", exc)
                raise

    async def pause(self, actor_id: int) -> None:
        current = time.time()
        async with self._lock:
            try:
                if not self.collection_ready:
                    await self.store.disconnect(current)
                await self.store.set_paused(True, actor_id, current)
                self._collection_since = current
                self.collection_ready = True
                self._recovered("pause")
            except Exception as exc:
                self._record_error("pause", exc)
                raise

    async def resume(
        self, actor_id: int, voice_channel_id: int | None,
        *, companions: frozenset[int] = frozenset(),
    ) -> None:
        current = time.time()
        async with self._lock:
            try:
                state = await self.store.state()
                if not state["paused"]:
                    return
                await self.store.set_paused(False, actor_id, current)
                self._collection_since = current
                self.collection_ready = False
                if self.connected and self.guild_is_available:
                    await self._start_guild_collection(voice_channel_id, current, companions)
                self._recovered("resume")
            except PermissionError:
                raise
            except Exception as exc:
                self._record_error("resume", exc)
                raise

    async def delete_data(self, actor_id: int) -> None:
        current = time.time()
        async with self._lock:
            try:
                await self.store.delete_data(actor_id, current)
                self._collection_since = current
                self.collection_ready = True
                self._recovered("data deletion")
            except Exception as exc:
                self._record_error("data deletion", exc)
                raise

    async def shutdown(self) -> None:
        """Close a cleanly observed visit and release the Store."""
        current = time.time()
        async with self._lock:
            if self._shutdown:
                return
            try:
                if self.connected and self.guild_is_available and self.collection_ready:
                    await self.store.checkpoint(current)
                # Also retry closure after a prior disconnect failure. A process
                # boundary ends coverage; keep any active visit incomplete so it
                # cannot claim a longest-visit record when the target may still
                # be connected.
                await self.store.disconnect(current)
            except Exception as exc:
                self._record_error("shutdown", exc)
                raise
            finally:
                self.connected = False
                self.guild_is_available = False
                self.collection_ready = False
                self._collection_since = None
                self._shutdown = True
                await self.store.close()
