"""Filtered Discord event collection with conservative connection recovery."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
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


# A voice snapshot, or a function that takes one. Passing the function lets the
# Tracker read Discord's cache only once it holds its lock, so a member who
# leaves while a command waits for the lock is not recorded as present.
VoiceSnapshot = Mapping[int, int] | Callable[[], Mapping[int, int]]


def _take_snapshot(snapshot: VoiceSnapshot) -> Mapping[int, int]:
    return snapshot() if callable(snapshot) else snapshot


def _allowed_voice_channel(config: Config, channel_id: int | None) -> int | None:
    if channel_id is None:
        return None
    allowlist = config.voice_channel_ids
    return channel_id if allowlist is None or channel_id in allowlist else None


def eligible_voice_channel_id(config: Config, guild: Any, channel: Any) -> int | None:
    """Return ``channel``'s ID when it is an eligible voice channel, else None.

    ``guild`` is the guild the channel was seen through. Eligible means both
    belong to the configured guild, the channel is not that guild's AFK
    channel, and it is inside ``VOICE_CHANNEL_IDS`` when an allowlist is set.
    """
    channel_id = _id(channel)
    if channel_id is None or _id(guild) != config.guild_id:
        return None
    channel_guild_id = _id(getattr(channel, "guild", None))
    if channel_guild_id is not None and channel_guild_id != config.guild_id:
        return None
    if _id(getattr(guild, "afk_channel", None)) == channel_id:
        return None
    return _allowed_voice_channel(config, channel_id)


# Operation names published in ``Tracker.last_error`` as "<operation> failed (...)".
OP_READY = "ready"
OP_GUILD_RECOVERY = "guild recovery"
OP_GUILD_UNAVAILABLE = "guild unavailable"
OP_DISCONNECT = "disconnect"
OP_MESSAGE = "message collection"
OP_VOICE = "voice collection"
OP_CHECKPOINT = "checkpoint"
OP_PAUSE = "pause"
OP_RESUME = "resume"
OP_DATA_DELETION = "data deletion"
OP_TRACK_USER = "track user"
OP_UNTRACK_USER = "untrack user"
OP_USER_DATA_DELETION = "user data deletion"
OP_SHUTDOWN = "shutdown"
# Failures that left collection stopped; a successful collection start clears them.
_COLLECTION_START_RECOVERS = (
    OP_DISCONNECT, OP_GUILD_UNAVAILABLE, OP_GUILD_RECOVERY, OP_READY, OP_VOICE,
)


class Tracker:
    """Serializes collector state and forwards eligible metadata to ``Store``.

    A *voice snapshot* is a mapping of member ID to the eligible voice channel
    ID that member is currently in (allowlist applied, AFK excluded, bots left
    out). The Discord adapter builds it; the tracker derives every tracked
    person's companions from it.
    """

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
        # Active tracked people. Refreshed from the Store whenever guild
        # collection starts and after every track, untrack, or deletion.
        self.tracked_ids: frozenset[int] = frozenset()

    @property
    def collection_since(self) -> float | None:
        """When guild collection last (re)started, or None while it is stopped."""
        return self._collection_since

    def report_error(self, operation: str, exc: BaseException) -> None:
        """Publish a failure, including adapter-owned ones such as maintenance."""
        # Error text from a dependency can include implementation details. Keep
        # status concise and safe; the adapter logs the traceback separately.
        self.last_error = f"{operation} failed ({type(exc).__name__})"
        self._error_operation = operation

    def report_recovered(self, operation: str) -> None:
        """Clear status only when the operation that failed has recovered."""
        if self._error_operation == operation:
            self.last_error = None
            self._error_operation = None

    @contextmanager
    def _operation(self, operation: str, *, recover: bool = True) -> Iterator[None]:
        """Report a failure of ``operation`` and re-raise it.

        Leaving the block normally (including by ``return``) clears an earlier
        failure of the same operation unless ``recover`` is False.
        """
        try:
            yield
        except Exception as exc:
            self.report_error(operation, exc)
            raise
        if recover:
            self.report_recovered(operation)

    def _collecting(self) -> bool:
        return (
            not self._shutdown
            and self.connected
            and self.guild_is_available
            and self.collection_ready
        )

    def _stop_collection(self) -> None:
        self.collection_ready = False
        self._collection_since = None

    @staticmethod
    def _now(now: float | None) -> float:
        return time.time() if now is None else float(now)

    async def _refresh_tracked(self) -> None:
        self.tracked_ids = frozenset(await self.store.active_user_ids())

    @staticmethod
    def _snapshot_companions(
        snapshot: Mapping[int, int], user_id: int, channel_id: int
    ) -> frozenset[int]:
        """Return the other members the snapshot places in ``channel_id``."""
        return frozenset(
            member_id for member_id, member_channel in snapshot.items()
            if member_channel == channel_id and member_id != user_id
        )

    async def _start_visit_from_snapshot(
        self, user_id: int, snapshot: Mapping[int, int], now: float
    ) -> None:
        """Begin an incomplete-start visit when the snapshot places a person in voice."""
        channel_id = _allowed_voice_channel(self.config, snapshot.get(user_id))
        if channel_id is None:
            return
        await self.store.voice_transition(
            user_id, channel_id, now,
            complete_start=False,
            companions=self._snapshot_companions(snapshot, user_id, channel_id),
        )

    async def _start_guild_collection(self, snapshot: VoiceSnapshot, now: float) -> None:
        self.collection_ready = False
        state = await self.store.state()
        self._collection_since = now
        await self._refresh_tracked()
        if state["paused"]:
            self.collection_ready = True
            return
        # A prior disconnect callback may have failed after Gateway state was
        # cleared. Retry closure before connect so an old open segment cannot
        # span the outage when Store.connect sees it as already open.
        await self.store.disconnect(now)
        await self.store.connect(now)
        members = _take_snapshot(snapshot)
        for user_id in sorted(self.tracked_ids):
            await self._start_visit_from_snapshot(user_id, members, now)
        self.collection_ready = True
        for operation in _COLLECTION_START_RECOVERS:
            self.report_recovered(operation)

    async def gateway_ready(self) -> None:
        """Record Gateway health before the configured guild becomes available."""
        async with self._lock:
            self.connected = True
            self._shutdown = False

    async def ready(self, snapshot: VoiceSnapshot, now: float | None = None) -> None:
        """Handle the initial ready event or a fresh session for the configured guild."""
        async with self._lock:
            current = self._now(now)
            with self._operation(OP_READY):
                self.connected = True
                self._shutdown = False
                self.guild_is_available = True
                self.collection_ready = False
                await self._start_guild_collection(snapshot, current)
                self.report_recovered(OP_GUILD_RECOVERY)

    async def guild_available(
        self, snapshot: VoiceSnapshot, now: float | None = None
    ) -> None:
        """Reconcile cached voice state when the configured guild returns."""
        async with self._lock:
            current = self._now(now)
            with self._operation(OP_GUILD_RECOVERY):
                was_available = self.guild_is_available
                self.guild_is_available = True
                if self.connected and (not was_available or not self.collection_ready):
                    await self._start_guild_collection(snapshot, current)

    async def guild_unavailable(self, now: float | None = None) -> None:
        """Stop collection while the configured guild is unavailable or removed."""
        current = self._now(now)
        async with self._lock:
            was_available = self.guild_is_available
            self.guild_is_available = False
            self._stop_collection()
            if not was_available:
                return
            with self._operation(OP_GUILD_UNAVAILABLE):
                # Store.disconnect closes voice at the last reliable checkpoint
                # and records the uncertain interval as a coverage gap.
                await self.store.disconnect(current)

    async def disconnected(self, now: float | None = None) -> None:
        """Handle Gateway loss. Voice and messages stay disabled until ready."""
        current = self._now(now)
        async with self._lock:
            was_collecting = self.connected and self.guild_is_available
            self.connected = False
            self.guild_is_available = False
            self._stop_collection()
            if not was_collecting:
                return
            with self._operation(OP_DISCONNECT):
                await self.store.disconnect(current)

    async def message(self, message: Any) -> bool:
        """Count one eligible message from a tracked person; return whether it was new."""
        inserted, _ = await self._message(message, reaction_eligible=False, ordinary=False)
        return inserted

    async def message_with_reaction(
        self, message: Any, *, ordinary: bool
    ) -> tuple[bool, bool]:
        """Count a message and return whether a reaction is due for it.

        The reaction countdown is the single global Leland-mode countdown, so
        the adapter uses this only for ``Config.leland_user_id``.
        """
        return await self._message(message, reaction_eligible=True, ordinary=ordinary)

    async def _message(
        self, message: Any, *, reaction_eligible: bool, ordinary: bool
    ) -> tuple[bool, bool]:
        async with self._lock:
            if not self._collecting():
                return False, False
            guild_id = _id(getattr(message, "guild", None))
            author = getattr(message, "author", None)
            author_id = _id(author)
            channel = getattr(message, "channel", None)
            message_id = _id(message)
            channel_id = _id(channel)
            created_at = _timestamp(getattr(message, "created_at", None))
            if (
                guild_id != self.config.guild_id
                or author_id not in self.tracked_ids
                or message_id is None
                or channel_id is None
                or created_at is None
                or bool(getattr(author, "bot", False))
            ):
                return False, False
            allowed = self.config.text_channel_ids
            if allowed is not None and channel_id not in allowed:
                return False, False
            # The Store rejects messages while paused or from before tracking
            # started; only the tracker knows when this collection run began.
            if self._collection_since is None or created_at < self._collection_since:
                return False, False
            with self._operation(OP_MESSAGE):
                if reaction_eligible:
                    return await self.store.add_message_with_reaction(
                        author_id, message_id, channel_id, created_at, ordinary=ordinary
                    )
                inserted = await self.store.add_message(
                    author_id, message_id, channel_id, created_at
                )
                return inserted, False

    def _channel_id_for_state(self, member: Any, state: Any) -> int | None:
        return eligible_voice_channel_id(
            self.config, getattr(member, "guild", None), getattr(state, "channel", None)
        )

    @staticmethod
    def _companions(channel: Any, member_id: int) -> frozenset[int]:
        """Return the humans in ``channel`` other than ``member_id``."""
        return frozenset(
            peer_id for peer in getattr(channel, "members", ())
            if not bool(getattr(peer, "bot", False))
            if (peer_id := _id(peer)) is not None
            if peer_id != member_id
        )

    async def voice(self, member: Any, before: Any, after: Any) -> None:
        """Process a member's channel change for them (if tracked) and for every roster."""
        async with self._lock:
            if not self._collecting():
                return
            guild_id = _id(getattr(member, "guild", None))
            member_id = _id(member)
            if guild_id != self.config.guild_id or member_id is None:
                return
            # Bots are never tracked and never count as company.
            if bool(getattr(member, "bot", False)):
                return
            previous = self._channel_id_for_state(member, before)
            current_channel = self._channel_id_for_state(member, after)
            if previous == current_channel:
                return
            with self._operation(OP_VOICE):
                try:
                    # The Store ignores voice and companion transitions while paused.
                    now = time.time()
                    if member_id in self.tracked_ids:
                        companions = (
                            self._companions(getattr(after, "channel", None), member_id)
                            if current_channel is not None else frozenset()
                        )
                        await self.store.voice_transition(
                            member_id, current_channel, now, companions=companions
                        )
                    # Everyone else already in either channel gains or loses this
                    # member as company; the Store skips rosters that already match.
                    if previous is not None:
                        await self.store.companion_transition(previous, member_id, False, now)
                    if current_channel is not None:
                        await self.store.companion_transition(current_channel, member_id, True, now)
                except Exception:
                    # A missed channel or roster change makes the persisted voice
                    # state unreliable. Stop crediting it until a current snapshot
                    # can be reconciled by the adapter's recovery loop.
                    self._stop_collection()
                    try:
                        await self.store.disconnect(time.time())
                    except Exception:
                        # Reconciliation retries disconnect before reopening any
                        # segments; preserve the original collection failure.
                        pass
                    raise

    async def checkpoint(self) -> None:
        """Persist the current reliable boundary when the guild is collectible."""
        async with self._lock:
            if not self._collecting():
                return
            with self._operation(OP_CHECKPOINT):
                await self.store.checkpoint(time.time())

    async def pause(self, actor_id: int) -> bool:
        """Pause collection; return False when it was already paused."""
        async with self._lock:
            current = time.time()
            with self._operation(OP_PAUSE):
                if (await self.store.state())["paused"]:
                    # Keep who paused it first and when.
                    return False
                if not self.collection_ready:
                    await self.store.disconnect(current)
                await self.store.set_paused(True, actor_id, current)
                self._collection_since = current
                self.collection_ready = True
                return True

    async def resume(self, actor_id: int, snapshot: VoiceSnapshot) -> bool:
        """Resume collection; return False when it was not paused."""
        async with self._lock:
            current = time.time()
            # Finding collection already running is not a recovery, so an
            # earlier resume failure stays visible in that case.
            with self._operation(OP_RESUME, recover=False):
                state = await self.store.state()
                if not state["paused"]:
                    return False
                await self.store.set_paused(False, actor_id, current)
                self._collection_since = current
                self.collection_ready = False
                if self.connected and self.guild_is_available:
                    await self._start_guild_collection(snapshot, current)
                self.report_recovered(OP_RESUME)
                return True

    async def delete_data(self, actor_id: int) -> None:
        async with self._lock:
            current = time.time()
            with self._operation(OP_DATA_DELETION):
                await self.store.delete_data(actor_id, current)
                self._collection_since = current
                self.collection_ready = True
                await self._refresh_tracked()

    async def track_user(
        self, user_id: int, actor_id: int, snapshot: VoiceSnapshot
    ) -> bool:
        """Start tracking a person; return False when they were already tracked.

        If collection is live and the snapshot places the person in an eligible
        channel, an incomplete-start visit begins at this moment: the bot did
        not observe them joining. The Store records no visit while paused.
        """
        async with self._lock:
            current = time.time()
            with self._operation(OP_TRACK_USER):
                added = await self.store.track_user(user_id, actor_id, current)
                await self._refresh_tracked()
                if added and self._collecting():
                    await self._start_visit_from_snapshot(
                        user_id, _take_snapshot(snapshot), current
                    )
                return added

    async def untrack_user(self, user_id: int, actor_id: int) -> bool:
        """Stop tracking a person now; an open visit ends incomplete, history stays."""
        async with self._lock:
            current = time.time()
            with self._operation(OP_UNTRACK_USER):
                removed = await self.store.untrack_user(user_id, actor_id, current)
                await self._refresh_tracked()
                return removed

    async def delete_user_data(self, user_id: int, actor_id: int) -> bool:
        """Erase one person's data and untrack them; collection for others continues.

        Deleting the configured Leland also switches the Leland-only evil and
        reaction modes off, as global deletion does.
        """
        async with self._lock:
            current = time.time()
            with self._operation(OP_USER_DATA_DELETION):
                existed = await self.store.delete_user_data(
                    user_id, actor_id, current,
                    reset_legacy_modes=(
                        self.config.leland_user_id is not None
                        and user_id == self.config.leland_user_id
                    ),
                )
                await self._refresh_tracked()
                return existed

    async def shutdown(self) -> None:
        """Close a cleanly observed visit and release the Store."""
        async with self._lock:
            current = time.time()
            if self._shutdown:
                return
            try:
                with self._operation(OP_SHUTDOWN, recover=False):
                    if self._collecting():
                        await self.store.checkpoint(current)
                    # Also retry closure after a prior disconnect failure. A
                    # process boundary ends coverage; keep any active visit
                    # incomplete so it cannot claim a longest-visit record when
                    # a tracked person may still be connected. The gap is the
                    # bot's own downtime, so it is labelled a restart rather
                    # than a lost Discord connection.
                    await self.store.disconnect(current, reason="process_restart")
            finally:
                self.connected = False
                self.guild_is_available = False
                self._stop_collection()
                self._shutdown = True
                await self.store.close()
