from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from flock_cctv.collectors import Tracker
from flock_cctv.config import Config
from flock_cctv.storage import Store


class FakeStore:
    """Records the collector's calls; people in ``active`` are the tracked list."""

    def __init__(
        self, *, paused: bool = False, paused_by: int | None = None,
        active: frozenset[int] = frozenset({20}),
    ) -> None:
        self.paused = paused
        self.paused_by = paused_by
        self.active: set[int] = set(active)
        self.tracking_since = 100.0
        self.last_checkpoint: float | None = None
        self.messages: dict[int, int] = {}  # message id -> author
        self.reaction_messages: list[tuple[int, int, bool]] = []
        # (user_id, channel_id, now, complete_start)
        self.transitions: list[tuple[int, int | None, float, bool]] = []
        self.companions: dict[int, frozenset[int]] = {}
        self.company_transitions: list[tuple[int, int, bool, float]] = []
        self.disconnections: list[float] = []
        self.disconnect_reasons: list[str] = []
        self.checkpoints: list[float] = []
        self.connections: list[float] = []
        self.track_calls: list[tuple[int, int, float]] = []
        self.untrack_calls: list[tuple[int, int, float]] = []
        self.user_deletions: list[tuple[int, int, float, bool]] = []
        self.deleted = False
        self.closed = False

    async def state(self):
        return {
            "paused": self.paused,
            "paused_by": str(self.paused_by) if self.paused_by is not None else None,
            "tracking_since": self.tracking_since,
            "last_checkpoint": self.last_checkpoint,
        }

    async def active_user_ids(self):
        return frozenset(self.active)

    async def connect(self, now):
        if not self.paused:
            self.connections.append(now)

    async def disconnect(self, now, reason="disconnect"):
        self.disconnections.append(now)
        self.disconnect_reasons.append(reason)

    # Like Store, record no voice, company, or message events while paused or
    # from before tracking started.
    async def voice_transition(
        self, user_id, channel_id, now, complete_start=True, companions=frozenset()
    ):
        if self.paused:
            return
        self.transitions.append((user_id, channel_id, now, complete_start))
        self.companions[user_id] = companions

    async def companion_transition(self, channel_id, member_id, joined, now):
        if self.paused:
            return
        self.company_transitions.append((channel_id, member_id, joined, now))

    async def add_message(self, user_id, message_id, channel_id, created_at):
        if self.paused or created_at < self.tracking_since or message_id in self.messages:
            return False
        self.messages[message_id] = user_id
        return True

    async def add_message_with_reaction(
        self, user_id, message_id, channel_id, created_at, *, ordinary
    ):
        self.reaction_messages.append((user_id, message_id, ordinary))
        return await self.add_message(user_id, message_id, channel_id, created_at), False

    async def checkpoint(self, now):
        self.checkpoints.append(now)
        self.last_checkpoint = now

    async def set_paused(self, paused, actor_id, now):
        self.paused = paused
        self.paused_by = actor_id if paused else None

    async def track_user(self, user_id, actor_id, now):
        self.track_calls.append((user_id, actor_id, now))
        if user_id in self.active:
            return False
        self.active.add(user_id)
        return True

    async def untrack_user(self, user_id, actor_id, now):
        self.untrack_calls.append((user_id, actor_id, now))
        if user_id not in self.active:
            return False
        self.active.discard(user_id)
        return True

    async def delete_user_data(self, user_id, actor_id, now, *, reset_legacy_modes=False):
        self.user_deletions.append((user_id, actor_id, now, reset_legacy_modes))
        existed = user_id in self.active
        self.active.discard(user_id)
        return existed

    async def delete_data(self, actor_id, now):
        self.deleted = True
        self.paused = True
        self.paused_by = actor_id
        self.messages.clear()

    async def close(self):
        self.closed = True


class RecoveringStore(FakeStore):
    """Model an open SQLite interval that survives a failed disconnect call."""

    def __init__(self, *, active: frozenset[int] = frozenset({20})):
        super().__init__(active=active)
        self.active_channels: dict[int, int] = {}
        self.store_connected = False
        self.fail_next_disconnect = False
        # (user_id, channel_id, closed_at)
        self.closed_voice: list[tuple[int, int, float]] = []
        self.operations: list[str] = []

    async def connect(self, now):
        self.operations.append("connect")
        if not self.paused:
            self.connections.append(now)
            self.store_connected = True

    async def disconnect(self, now, reason="disconnect"):
        self.operations.append("disconnect")
        if self.fail_next_disconnect:
            self.fail_next_disconnect = False
            raise OSError("temporary storage failure")
        self.disconnections.append(now)
        for user_id, channel_id in sorted(self.active_channels.items()):
            closed_at = self.last_checkpoint or now
            self.closed_voice.append((user_id, channel_id, closed_at))
            self.transitions.append((user_id, None, closed_at, False))
        self.active_channels.clear()
        self.store_connected = False

    async def voice_transition(
        self, user_id, channel_id, now, complete_start=True, companions=frozenset()
    ):
        self.operations.append("voice")
        if self.active_channels.get(user_id) == channel_id:
            return
        self.transitions.append((user_id, channel_id, now, complete_start))
        self.companions[user_id] = companions
        if channel_id is None:
            self.active_channels.pop(user_id, None)
        else:
            self.active_channels[user_id] = channel_id

    async def checkpoint(self, now):
        self.operations.append("checkpoint")
        await super().checkpoint(now)


def make_config(
    *,
    text_channel_ids: frozenset[int] | None = None,
    voice_channel_ids: frozenset[int] | None = None,
    leland_user_id: int | None = None,
) -> Config:
    return Config(
        token="hidden-token",
        guild_id=10,
        owner_user_id=99,
        output_channel_id=None,
        text_channel_ids=text_channel_ids,
        voice_channel_ids=voice_channel_ids,
        timezone="UTC",
        database_path=Path("/tmp/test-tracker.sqlite3"),
        backup_dir=Path("/tmp/test-tracker-backups"),
        leland_user_id=leland_user_id,
    )


def message(*, message_id=101, guild_id=10, user_id=20, channel_id=30, created=200.0, bot=False):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=guild_id),
        author=SimpleNamespace(id=user_id, bot=bot),
        channel=SimpleNamespace(id=channel_id),
        created_at=datetime.fromtimestamp(created, tz=timezone.utc),
    )


GUILD = SimpleNamespace(id=10, afk_channel=None)


def voice_channel(channel_id: int, *member_ids: int, bots: tuple[int, ...] = ()):
    """A cached voice channel whose ``members`` list is already updated."""
    members = [SimpleNamespace(id=member_id, bot=False) for member_id in member_ids]
    members.extend(SimpleNamespace(id=member_id, bot=True) for member_id in bots)
    return SimpleNamespace(id=channel_id, guild=GUILD, members=members)


def member(member_id: int, *, bot: bool = False):
    return SimpleNamespace(id=member_id, guild=GUILD, bot=bot)


NO_CHANNEL = SimpleNamespace(channel=None)


def in_channel(channel):
    return SimpleNamespace(channel=channel)


def roster(path: Path, user_id: int) -> list[int] | None:
    """Read a person's persisted live company roster straight from SQLite."""
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT member_ids FROM voice_company_current WHERE user_id = ?", (str(user_id),)
        ).fetchone()
    return None if row is None else [int(value) for value in json.loads(row[0])]


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_waiting_for_lock_samples_time_with_snapshot(self):
        for operation in ("ready", "guild_available"):
            with self.subTest(operation=operation):
                store = FakeStore()
                tracker = Tracker(make_config(), store)
                await tracker.gateway_ready()
                await tracker._lock.acquire()
                with patch("flock_cctv.collectors.time.time", return_value=180.0):
                    recovering = asyncio.create_task(getattr(tracker, operation)({20: 40}))
                    await asyncio.sleep(0)
                with patch("flock_cctv.collectors.time.time", return_value=200.0):
                    tracker._lock.release()
                    await recovering
                self.assertEqual(store.connections, [200.0])
                self.assertEqual(store.transitions, [(20, 40, 200.0, False)])
                self.assertFalse(await tracker.message(message(created=190.0)))

    async def test_resume_waiting_for_lock_uses_actual_resume_boundary(self):
        store = FakeStore(paused=True)
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150.0)
        await tracker._lock.acquire()
        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            resuming = asyncio.create_task(tracker.resume(99, {20: 40}))
            await asyncio.sleep(0)
        with patch("flock_cctv.collectors.time.time", return_value=200.0):
            tracker._lock.release()
            await resuming
        self.assertEqual(store.transitions, [(20, 40, 200.0, False)])
        self.assertFalse(await tracker.message(message(created=190.0)))
        self.assertTrue(await tracker.message(message(created=201.0)))

    async def test_gateway_resume_continues_visit_with_real_store(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config()
            store = Store(root / "tracker.sqlite3", root / "backups", "UTC")
            await store.initialize(100.0, config.guild_id)
            await store.track_user(20, 99, 100.0)
            tracker = Tracker(config, store)
            try:
                await tracker.ready({}, now=100.0)
                self.assertEqual(tracker.tracked_ids, frozenset({20}))
                await store.voice_transition(20, 500, 110.0)
                with patch("flock_cctv.collectors.time.time", return_value=170.0):
                    await tracker.checkpoint()
                await tracker.disconnected(now=175.0)
                await tracker.ready({20: 500}, now=200.0)
                await store.voice_transition(20, None, 300.0)
                record = await store.records(20, 300.0)
                self.assertAlmostEqual(record["longest_visit_seconds"], 160)
                self.assertEqual(record["longest_visit_at"], 110.0)
                self.assertEqual((await store.stats(20, "all", 300.0))["voice_visits"], 1)
            finally:
                await store.close()

    async def test_paused_real_store_records_no_messages_or_voice(self):
        # The tracker relies on Store to drop events while paused.
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config()
            store = Store(root / "tracker.sqlite3", root / "backups", "UTC")
            await store.initialize(100.0, config.guild_id)
            await store.track_user(20, 99, 100.0)
            await store.track_user(21, 99, 100.0)
            tracker = Tracker(config, store)
            try:
                await tracker.ready({21: 40}, now=150.0)
                with patch("flock_cctv.collectors.time.time", return_value=160.0):
                    await tracker.pause(99)
                self.assertTrue(tracker.collection_ready)
                self.assertFalse(await tracker.message(message(created=170.0)))
                with patch("flock_cctv.collectors.time.time", return_value=180.0):
                    await tracker.voice(
                        member(20), NO_CHANNEL, in_channel(voice_channel(40, 20, 21)),
                    )
                self.assertIsNone(tracker.last_error)
                stats = await store.stats(20, "all", 200.0)
                self.assertEqual(stats["messages"], 0)
                self.assertEqual(stats["voice_visits"], 0)
                self.assertEqual(stats["voice_seconds"], 0)
                self.assertIsNone(roster(root / "tracker.sqlite3", 20))
                self.assertIsNone(roster(root / "tracker.sqlite3", 21))
            finally:
                await store.close()

    async def test_message_filter_deduplicates_and_rejects_old_events(self):
        store = FakeStore()
        tracker = Tracker(make_config(text_channel_ids=frozenset({30})), store)
        await tracker.ready({}, now=150.0)

        self.assertTrue(await tracker.message(message(created=151.0)))
        self.assertFalse(await tracker.message(message(created=151.0)))
        self.assertFalse(await tracker.message(message(message_id=102, user_id=21)))
        self.assertFalse(await tracker.message(message(message_id=103, guild_id=11)))
        self.assertFalse(await tracker.message(message(message_id=104, channel_id=31)))
        self.assertFalse(await tracker.message(message(message_id=105, created=149.0)))
        self.assertFalse(await tracker.message(message(message_id=106, bot=True)))
        self.assertEqual(store.messages, {101: 20})

    async def test_messages_route_to_each_tracked_author_and_ignore_untracked_ones(self):
        store = FakeStore(active=frozenset({20, 21}))
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150.0)

        self.assertTrue(await tracker.message(message(message_id=1, user_id=20, created=151.0)))
        self.assertTrue(await tracker.message(message(message_id=2, user_id=21, created=152.0)))
        self.assertFalse(await tracker.message(message(message_id=3, user_id=22, created=153.0)))
        self.assertEqual(store.messages, {1: 20, 2: 21})

        # The reaction variant forwards the author's own ID and the same filters.
        self.assertEqual(
            await tracker.message_with_reaction(
                message(message_id=4, user_id=21, created=154.0), ordinary=True
            ),
            (True, False),
        )
        self.assertEqual(
            await tracker.message_with_reaction(
                message(message_id=5, user_id=22, created=155.0), ordinary=True
            ),
            (False, False),
        )
        self.assertEqual(store.reaction_messages, [(21, 4, True)])
        self.assertEqual(store.messages, {1: 20, 2: 21, 4: 21})

    async def test_nobody_is_tracked_by_default_so_no_message_is_counted(self):
        store = FakeStore(active=frozenset())
        tracker = Tracker(make_config(), store)
        await tracker.ready({7: 40}, now=150.0)
        self.assertEqual(tracker.tracked_ids, frozenset())
        self.assertFalse(await tracker.message(message(created=151.0)))
        self.assertEqual(store.messages, {})
        self.assertEqual(store.transitions, [])

    async def test_paused_messages_and_voice_are_not_collected(self):
        store = FakeStore(paused=True, paused_by=99)
        tracker = Tracker(make_config(), store)
        await tracker.ready({20: 40}, now=150.0)
        self.assertTrue(tracker.collection_ready)
        self.assertEqual(store.connections, [])
        self.assertEqual(store.transitions, [])
        self.assertFalse(await tracker.message(message(created=151.0)))
        await tracker.voice(
            member(20), NO_CHANNEL, in_channel(voice_channel(40, 20)),
        )
        self.assertEqual(store.transitions, [])
        self.assertEqual(store.company_transitions, [])

    async def test_disconnect_and_guild_unavailable_stop_collection_until_reconciled(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({20: 40}, now=150.0)
        self.assertTrue(tracker.connected)
        self.assertTrue(tracker.guild_is_available)
        self.assertEqual(store.transitions, [(20, 40, 150.0, False)])

        await tracker.guild_unavailable(now=175.0)
        self.assertTrue(tracker.connected)
        self.assertFalse(tracker.guild_is_available)
        before = len(store.transitions)
        await tracker.voice(
            member(20), NO_CHANNEL, in_channel(voice_channel(41, 20)),
        )
        self.assertEqual(len(store.transitions), before)
        self.assertEqual(store.company_transitions, [])

        await tracker.guild_available({20: 41}, now=200.0)
        self.assertTrue(tracker.guild_is_available)
        self.assertEqual(store.transitions[-1], (20, 41, 200.0, False))

        await tracker.disconnected(now=210.0)
        self.assertFalse(tracker.connected)
        self.assertFalse(tracker.guild_is_available)
        self.assertFalse(await tracker.message(message(message_id=107, created=220.0)))
        await tracker.ready({}, now=300.0)
        self.assertTrue(tracker.connected)
        self.assertTrue(tracker.guild_is_available)

    async def test_reconnect_retries_failed_disconnect_before_opening_coverage(self):
        store = RecoveringStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({20: 40}, now=150.0)
        with patch("flock_cctv.collectors.time.time", return_value=170.0):
            await tracker.checkpoint()

        store.fail_next_disconnect = True
        with self.assertRaises(OSError):
            await tracker.disconnected(now=200.0)
        self.assertEqual(store.active_channels, {20: 40})

        await tracker.ready({20: 40}, now=300.0)
        self.assertEqual(store.closed_voice, [(20, 40, 170.0)])
        self.assertEqual(store.active_channels, {20: 40})
        self.assertEqual(store.transitions[-1], (20, 40, 300.0, False))
        self.assertEqual(store.operations[-3:], ["disconnect", "connect", "voice"])

    async def test_voice_failure_with_failed_disconnect_blocks_checkpoints_until_retry(self):
        store = RecoveringStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({20: 40}, now=150.0)
        with patch("flock_cctv.collectors.time.time", return_value=170.0):
            await tracker.checkpoint()
        store.fail_next_disconnect = True
        with patch.object(store, "voice_transition", new=AsyncMock(side_effect=OSError("disk"))):
            with self.assertRaises(OSError):
                await tracker.voice(member(20), in_channel(voice_channel(40, 20)), NO_CHANNEL)
        self.assertFalse(tracker.collection_ready)
        self.assertEqual(store.active_channels, {20: 40})
        with patch("flock_cctv.collectors.time.time", return_value=300.0):
            await tracker.checkpoint()
        self.assertEqual(store.checkpoints, [170.0])
        await tracker.guild_available({}, now=310.0)
        self.assertTrue(tracker.collection_ready)
        self.assertEqual(store.closed_voice, [(20, 40, 170.0)])
        self.assertIsNone(tracker.last_error)

    async def test_voice_filters_guild_allowlist_and_afk_channel(self):
        store = FakeStore()
        tracker = Tracker(make_config(voice_channel_ids=frozenset({40, 41})), store)
        await tracker.ready({}, now=150.0)
        afk_guild = SimpleNamespace(id=10, afk_channel=SimpleNamespace(id=99))
        tracked = SimpleNamespace(id=20, guild=afk_guild, bot=False)
        other_guild = SimpleNamespace(id=11, afk_channel=None)

        def channel(channel_id, guild=afk_guild):
            return SimpleNamespace(id=channel_id, guild=guild, members=[])

        with patch("flock_cctv.collectors.time.time", return_value=160.0):
            await tracker.voice(tracked, NO_CHANNEL, in_channel(channel(40)))
            await tracker.voice(tracked, in_channel(channel(40)), in_channel(channel(41)))
            await tracker.voice(tracked, in_channel(channel(41)), in_channel(channel(99)))
            # A channel outside the allowlist and one from another guild never count.
            await tracker.voice(tracked, in_channel(channel(99)), in_channel(channel(42)))
            await tracker.voice(
                tracked, NO_CHANNEL, in_channel(channel(40, other_guild)),
            )
            await tracker.voice(
                SimpleNamespace(id=20, guild=other_guild, bot=False),
                NO_CHANNEL, in_channel(channel(40, other_guild)),
            )
        self.assertEqual(
            store.transitions,
            [(20, 40, 160.0, True), (20, 41, 160.0, True), (20, None, 160.0, True)],
        )
        self.assertEqual(
            store.company_transitions,
            [(40, 20, True, 160.0), (40, 20, False, 160.0), (41, 20, True, 160.0),
             (41, 20, False, 160.0)],
        )

    async def test_untracked_member_updates_rosters_but_never_opens_a_visit(self):
        store = FakeStore()
        tracker = Tracker(make_config(voice_channel_ids=frozenset({40})), store)
        await tracker.ready({20: 40, 21: 40}, now=150)
        self.assertEqual(store.transitions, [(20, 40, 150, False)])
        self.assertEqual(store.companions[20], frozenset({21}))
        peer = member(22)
        channel = voice_channel(40, 20, 21, 22)
        with patch("flock_cctv.collectors.time.time", return_value=160):
            await tracker.voice(peer, NO_CHANNEL, in_channel(channel))
            await tracker.voice(peer, in_channel(channel), NO_CHANNEL)
            await tracker.voice(
                member(23, bot=True), NO_CHANNEL, in_channel(voice_channel(40, 23)),
            )
        self.assertEqual(store.transitions, [(20, 40, 150, False)])
        self.assertEqual(store.company_transitions, [(40, 22, True, 160), (40, 22, False, 160)])

    async def test_tracked_member_voice_event_has_companions_and_updates_other_rosters(self):
        store = FakeStore(active=frozenset({20, 21}))
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150)
        channel = voice_channel(40, 20, 21, 22, bots=(23,))
        with patch("flock_cctv.collectors.time.time", return_value=160):
            await tracker.voice(member(20), NO_CHANNEL, in_channel(channel))
        # Every other human present is company (tracked or not); bots never are.
        self.assertEqual(store.transitions, [(20, 40, 160, True)])
        self.assertEqual(store.companions[20], frozenset({21, 22}))
        # Other tracked rosters in that channel hear about the join with the same clock.
        self.assertEqual(store.company_transitions, [(40, 20, True, 160)])

        with patch("flock_cctv.collectors.time.time", return_value=170):
            await tracker.voice(member(20), in_channel(channel), NO_CHANNEL)
        self.assertEqual(store.transitions[-1], (20, None, 170, True))
        self.assertEqual(store.company_transitions[-1], (40, 20, False, 170))

    async def test_tracked_member_moving_channels_leaves_one_roster_and_joins_another(self):
        store = FakeStore(active=frozenset({20, 21}))
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150)
        first = voice_channel(40, 21)
        second = voice_channel(41, 22, 20)
        times = iter([160, 160, 160])
        with patch("flock_cctv.collectors.time.time", side_effect=lambda: next(times)):
            await tracker.voice(member(20), in_channel(first), in_channel(second))
        self.assertEqual(store.transitions, [(20, 41, 160, True)])
        self.assertEqual(store.companions[20], frozenset({22}))
        self.assertEqual(
            store.company_transitions, [(40, 20, False, 160), (41, 20, True, 160)]
        )

    async def test_one_clock_reading_covers_the_whole_voice_event(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150)
        readings = iter([160.0, 161.0, 162.0])
        with patch("flock_cctv.collectors.time.time", side_effect=lambda: next(readings)):
            await tracker.voice(
                member(20), in_channel(voice_channel(40, 20)), in_channel(voice_channel(41, 20)),
            )
        stamps = {item[2] for item in store.transitions} | {
            item[3] for item in store.company_transitions
        }
        self.assertEqual(stamps, {160.0})

    async def test_bot_voice_events_are_ignored_entirely(self):
        store = FakeStore(active=frozenset({20}))
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150)
        bot = member(30, bot=True)
        await tracker.voice(bot, NO_CHANNEL, in_channel(voice_channel(40, 30)))
        self.assertEqual(store.transitions, [])
        self.assertEqual(store.company_transitions, [])

    async def test_same_eligible_channel_event_does_nothing(self):
        store = FakeStore()
        tracker = Tracker(make_config(voice_channel_ids=frozenset({40})), store)
        await tracker.ready({}, now=150)
        channel = voice_channel(40, 20)
        # Mute/deafen updates keep the same channel; so does a move between
        # two ineligible channels.
        await tracker.voice(member(20), in_channel(channel), in_channel(channel))
        await tracker.voice(
            member(20), in_channel(voice_channel(50, 20)), in_channel(voice_channel(51, 20)),
        )
        self.assertEqual(store.transitions, [])
        self.assertEqual(store.company_transitions, [])

    async def test_ready_starts_incomplete_visit_for_every_tracked_person_in_snapshot(self):
        store = FakeStore(active=frozenset({20, 21, 23}))
        tracker = Tracker(make_config(), store)
        snapshot = {20: 40, 21: 40, 22: 40, 24: 41}
        await tracker.ready(snapshot, now=150.0)
        # Person 23 is tracked but not in voice; 24 is in voice but not tracked.
        self.assertEqual(
            store.transitions, [(20, 40, 150.0, False), (21, 40, 150.0, False)]
        )
        self.assertEqual(store.companions[20], frozenset({21, 22}))
        self.assertEqual(store.companions[21], frozenset({20, 22}))
        self.assertEqual(store.connections, [150.0])
        self.assertEqual(tracker.tracked_ids, frozenset({20, 21, 23}))

    async def test_snapshot_is_reapplied_against_voice_allowlist(self):
        store = FakeStore(active=frozenset({20, 21}))
        tracker = Tracker(make_config(voice_channel_ids=frozenset({40})), store)
        await tracker.ready({20: 40, 21: 41}, now=150.0)
        self.assertEqual(store.transitions, [(20, 40, 150.0, False)])

    async def test_guild_available_starts_visits_for_everyone_in_snapshot(self):
        store = FakeStore(active=frozenset({20, 21}))
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150.0)
        await tracker.guild_unavailable(now=160.0)
        await tracker.guild_available({20: 40, 21: 41, 22: 40}, now=200.0)
        self.assertEqual(
            store.transitions[-2:], [(20, 40, 200.0, False), (21, 41, 200.0, False)]
        )
        self.assertEqual(store.companions[20], frozenset({22}))
        self.assertEqual(store.companions[21], frozenset())

    async def test_tracked_ids_refresh_when_collection_restarts(self):
        store = FakeStore(active=frozenset({20}))
        tracker = Tracker(make_config(), store)
        self.assertEqual(tracker.tracked_ids, frozenset())
        await tracker.ready({}, now=150.0)
        self.assertEqual(tracker.tracked_ids, frozenset({20}))
        store.active.add(21)  # Changed behind the collector's back.
        self.assertFalse(await tracker.message(message(message_id=9, user_id=21, created=151.0)))
        await tracker.disconnected(now=160.0)
        await tracker.ready({}, now=200.0)
        self.assertEqual(tracker.tracked_ids, frozenset({20, 21}))
        self.assertTrue(await tracker.message(message(message_id=10, user_id=21, created=201.0)))

    async def test_failed_reconciliation_blocks_writes_until_retry_succeeds(self):
        store = RecoveringStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({20: 40}, now=150)
        with patch("flock_cctv.collectors.time.time", return_value=170):
            await tracker.checkpoint()
        store.fail_next_disconnect = True
        with self.assertRaises(OSError):
            await tracker.disconnected(now=200)
        store.fail_next_disconnect = True
        with self.assertRaises(OSError):
            await tracker.ready({20: 40}, now=300)
        self.assertFalse(tracker.collection_ready)
        self.assertFalse(await tracker.message(message(created=310)))
        before_operations = list(store.operations)
        await tracker.checkpoint()
        await tracker.voice(
            member(20), in_channel(voice_channel(40, 20)), NO_CHANNEL,
        )
        self.assertEqual(store.operations, before_operations)
        await tracker.guild_available({20: 40}, now=400)
        self.assertTrue(tracker.collection_ready)
        self.assertEqual(store.closed_voice, [(20, 40, 170)])
        self.assertTrue(await tracker.message(message(created=410)))

    async def test_shutdown_does_not_checkpoint_failed_reconciliation(self):
        store = RecoveringStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({20: 40}, now=150)
        with patch("flock_cctv.collectors.time.time", return_value=170):
            await tracker.checkpoint()
        store.fail_next_disconnect = True
        with self.assertRaises(OSError):
            await tracker.ready({20: 40}, now=300)
        with patch("flock_cctv.collectors.time.time", return_value=400):
            await tracker.shutdown()
        self.assertEqual(store.checkpoints, [170])
        self.assertEqual(store.closed_voice, [(20, 40, 170)])

    async def test_admin_can_resume_legacy_pause_by_anyone(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150)
        await tracker.pause(99)
        await tracker.pause(20)
        await tracker.resume(99, {})
        self.assertFalse(store.paused)

    async def test_pausing_twice_keeps_the_first_pause_and_reports_no_change(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150)
        self.assertTrue(await tracker.pause(99))
        self.assertFalse(await tracker.pause(20))
        self.assertEqual(store.paused_by, 99)
        self.assertTrue(await tracker.resume(20, {}))
        self.assertFalse(await tracker.resume(20, {}))

    async def test_admin_resume_reconciles_incomplete_visits_for_everyone(self):
        store = FakeStore(active=frozenset({20, 21}))
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150.0)
        with patch("flock_cctv.collectors.time.time", return_value=160.0):
            await tracker.pause(20)
        self.assertTrue(store.paused)
        await tracker.disconnected(now=170.0)
        with patch("flock_cctv.collectors.time.time", return_value=200.0):
            await tracker.resume(99, {20: 40, 21: 40})
        self.assertFalse(store.paused)
        self.assertFalse(tracker.guild_is_available)
        await tracker.gateway_ready()
        await tracker.guild_available({20: 40, 21: 40}, now=210.0)
        self.assertEqual(
            store.transitions[-2:], [(20, 40, 210.0, False), (21, 40, 210.0, False)]
        )
        self.assertEqual(store.companions[20], frozenset({21}))
        self.assertEqual(store.companions[21], frozenset({20}))

    async def test_resume_with_live_gateway_starts_visits_at_resume_time(self):
        store = FakeStore(active=frozenset({20, 21}))
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150.0)
        with patch("flock_cctv.collectors.time.time", return_value=160.0):
            await tracker.pause(99)
        with patch("flock_cctv.collectors.time.time", return_value=200.0):
            await tracker.resume(99, {20: 40, 21: 40, 22: 40})
        self.assertFalse(store.paused)
        self.assertTrue(tracker.collection_ready)
        self.assertEqual(
            store.transitions, [(20, 40, 200.0, False), (21, 40, 200.0, False)]
        )
        self.assertEqual(store.companions[20], frozenset({21, 22}))
        # Resuming something that is not paused is a no-op.
        before = list(store.transitions)
        await tracker.resume(99, {20: 40})
        self.assertEqual(store.transitions, before)

    async def test_shutdown_truncates_active_visit_and_checkpoint_does_not_clear_other_health_error(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready({20: 40}, now=150.0)
        tracker.report_error("maintenance", OSError("disk failure"))
        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            await tracker.shutdown()
        self.assertEqual(store.checkpoints, [180.0])
        self.assertEqual(store.disconnections, [150.0, 180.0])
        # Startup clears a stale session as a lost connection; the bot's own
        # shutdown is labelled a restart.
        self.assertEqual(store.disconnect_reasons, ["disconnect", "process_restart"])
        self.assertEqual(store.transitions, [(20, 40, 150.0, False)])
        self.assertTrue(store.closed)
        self.assertEqual(tracker.last_error, "maintenance failed (OSError)")
        self.assertIsNone(tracker.collection_since)


class TrackedListTests(unittest.IsolatedAsyncioTestCase):
    async def live_tracker(self, **store_kwargs):
        store = FakeStore(**store_kwargs)
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150.0)
        return store, tracker

    async def test_track_user_mid_call_starts_incomplete_visit_with_companions(self):
        store, tracker = await self.live_tracker(active=frozenset({20}))
        snapshot = {20: 40, 22: 40, 23: 40, 24: 41}
        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            self.assertTrue(await tracker.track_user(22, 99, snapshot))
        self.assertEqual(store.track_calls, [(22, 99, 180.0)])
        self.assertEqual(tracker.tracked_ids, frozenset({20, 22}))
        self.assertEqual(store.transitions, [(22, 40, 180.0, False)])
        self.assertEqual(store.companions[22], frozenset({20, 23}))
        # Their own messages count from now on.
        self.assertTrue(await tracker.message(message(message_id=7, user_id=22, created=181.0)))

    async def test_snapshot_functions_are_read_only_while_holding_the_lock(self):
        store, tracker = await self.live_tracker(active=frozenset({20}))
        seen = []

        def take():
            seen.append(tracker._lock.locked())
            return {22: 40}

        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            self.assertTrue(await tracker.track_user(22, 99, take))
        self.assertEqual(seen, [True])
        self.assertEqual(store.transitions, [(22, 40, 180.0, False)])
        await tracker.ready(take, now=200.0)
        self.assertEqual(seen, [True, True])

    async def test_track_user_not_in_voice_only_updates_the_tracked_set(self):
        store, tracker = await self.live_tracker()
        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            self.assertTrue(await tracker.track_user(22, 99, {20: 40}))
        self.assertEqual(store.transitions, [])
        self.assertEqual(tracker.tracked_ids, frozenset({20, 22}))

    async def test_track_user_already_tracked_returns_false_without_new_visit(self):
        store, tracker = await self.live_tracker()
        self.assertFalse(await tracker.track_user(20, 99, {20: 40}))
        self.assertEqual(store.transitions, [])
        self.assertEqual(tracker.tracked_ids, frozenset({20}))

    async def test_track_user_does_not_start_visit_while_paused(self):
        store, tracker = await self.live_tracker()
        await tracker.pause(99)
        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            self.assertTrue(await tracker.track_user(22, 99, {22: 40}))
        self.assertEqual(store.transitions, [])
        self.assertEqual(tracker.tracked_ids, frozenset({20, 22}))
        # Resuming later reconciles the person from the fresh snapshot.
        with patch("flock_cctv.collectors.time.time", return_value=200.0):
            await tracker.resume(99, {22: 40})
        self.assertEqual(store.transitions, [(22, 40, 200.0, False)])

    async def test_track_user_does_not_start_visit_when_collection_is_not_live(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        # Not connected yet: the person is tracked, but nothing is observed.
        self.assertTrue(await tracker.track_user(22, 99, {22: 40}))
        self.assertEqual(store.transitions, [])
        self.assertEqual(tracker.tracked_ids, frozenset({20, 22}))

        await tracker.ready({}, now=150.0)
        await tracker.guild_unavailable(now=160.0)
        self.assertTrue(await tracker.track_user(23, 99, {23: 40}))
        self.assertEqual(store.transitions, [])

        tracker.guild_is_available = True
        tracker.collection_ready = False  # Reconciliation pending.
        self.assertTrue(await tracker.track_user(24, 99, {24: 40}))
        self.assertEqual(store.transitions, [])
        # The next reconciliation picks all of them up.
        await tracker.guild_available({22: 40, 23: 40, 24: 40}, now=200.0)
        self.assertEqual(
            [item[0] for item in store.transitions], [22, 23, 24]
        )

    async def test_track_user_respects_voice_allowlist(self):
        store = FakeStore()
        tracker = Tracker(make_config(voice_channel_ids=frozenset({40})), store)
        await tracker.ready({}, now=150.0)
        self.assertTrue(await tracker.track_user(22, 99, {22: 41}))
        self.assertEqual(store.transitions, [])

    async def test_untrack_user_refreshes_tracked_ids_and_stops_messages(self):
        store, tracker = await self.live_tracker(active=frozenset({20, 21}))
        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            self.assertTrue(await tracker.untrack_user(21, 99))
        self.assertEqual(store.untrack_calls, [(21, 99, 180.0)])
        self.assertEqual(tracker.tracked_ids, frozenset({20}))
        self.assertFalse(await tracker.message(message(message_id=8, user_id=21, created=181.0)))
        self.assertFalse(await tracker.untrack_user(21, 99))
        # Voice of the untracked person no longer opens a visit, but still
        # shows up as company for the people who remain tracked.
        with patch("flock_cctv.collectors.time.time", return_value=190.0):
            await tracker.voice(member(21), NO_CHANNEL, in_channel(voice_channel(40, 21)))
        self.assertEqual(store.transitions, [])
        self.assertEqual(store.company_transitions, [(40, 21, True, 190.0)])

    async def test_delete_user_data_resets_legacy_modes_only_for_leland(self):
        store = FakeStore(active=frozenset({20, 21}))
        tracker = Tracker(make_config(leland_user_id=20), store)
        await tracker.ready({}, now=150.0)
        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            self.assertTrue(await tracker.delete_user_data(21, 99))
            self.assertTrue(await tracker.delete_user_data(20, 99))
            self.assertFalse(await tracker.delete_user_data(30, 99))
        self.assertEqual(
            store.user_deletions,
            [(21, 99, 180.0, False), (20, 99, 180.0, True), (30, 99, 180.0, False)],
        )
        self.assertEqual(tracker.tracked_ids, frozenset())
        # Collection for everyone else keeps running (global state untouched).
        self.assertFalse(store.paused)
        self.assertTrue(tracker.collection_ready)

    async def test_delete_user_data_without_leland_configured_never_resets_modes(self):
        store = FakeStore(active=frozenset({20}))
        tracker = Tracker(make_config(), store)
        await tracker.ready({}, now=150.0)
        await tracker.delete_user_data(20, 99)
        self.assertEqual([item[3] for item in store.user_deletions], [False])

    async def test_delete_data_refreshes_tracked_ids(self):
        store, tracker = await self.live_tracker(active=frozenset({20, 21}))
        original = store.delete_data

        async def delete_and_drop(actor_id, now):
            await original(actor_id, now)
            store.active.discard(21)  # e.g. an inactive row removed by the Store

        store.delete_data = delete_and_drop
        await tracker.delete_data(99)
        self.assertTrue(store.deleted)
        self.assertEqual(tracker.tracked_ids, frozenset({20}))

    async def test_store_failures_are_recorded_and_recover(self):
        store, tracker = await self.live_tracker()

        async def fail(*args, **kwargs):
            raise OSError("disk")

        original = store.track_user
        store.track_user = fail
        with self.assertRaises(OSError):
            await tracker.track_user(22, 99, {})
        self.assertEqual(tracker.last_error, "track user failed (OSError)")
        store.track_user = original
        await tracker.track_user(22, 99, {})
        self.assertIsNone(tracker.last_error)


class MultiPersonStoreTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end collector behaviour against a real Store with two tracked people."""

    async def asyncSetUp(self):
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.database = self.root / "tracker.sqlite3"
        self.config = make_config()
        self.store = Store(self.database, self.root / "backups", "UTC")
        await self.store.initialize(100.0, self.config.guild_id)
        await self.store.track_user(20, 99, 100.0)
        await self.store.track_user(21, 99, 100.0)
        self.tracker = Tracker(self.config, self.store)

    async def asyncTearDown(self):
        await self.store.close()
        self._directory.cleanup()

    async def voice(self, now, who, before, after):
        with patch("flock_cctv.collectors.time.time", return_value=now):
            await self.tracker.voice(who, before, after)

    async def test_failed_voice_leave_stops_credit_until_snapshot_recovery(self):
        await self.tracker.ready({20: 40, 21: 40}, now=100.0)
        with patch("flock_cctv.collectors.time.time", return_value=110.0):
            await self.tracker.checkpoint()
        with patch.object(self.store, "voice_transition", new=AsyncMock(side_effect=OSError("disk"))):
            with self.assertRaises(OSError):
                await self.voice(120.0, member(20), in_channel(voice_channel(40, 20, 21)), NO_CHANNEL)
        self.assertFalse(self.tracker.collection_ready)
        self.assertEqual(self.tracker.last_error, "voice collection failed (OSError)")
        with patch("flock_cctv.collectors.time.time", return_value=130.0):
            await self.tracker.checkpoint()
        for user_id in (20, 21):
            self.assertEqual((await self.store.stats(user_id, "all", 130.0))["voice_seconds"], 10.0)
        await self.tracker.guild_available({21: 40}, now=140.0)
        self.assertTrue(self.tracker.collection_ready)
        self.assertIsNone(self.tracker.last_error)
        self.assertEqual(roster(self.database, 21), [])
        self.assertIsNone(roster(self.database, 20))
        self.assertEqual((await self.store.stats(20, "all", 150.0))["voice_seconds"], 10.0)
        self.assertEqual((await self.store.stats(21, "all", 150.0))["voice_seconds"], 20.0)

    async def test_failed_untracked_companion_leave_stops_stale_roster_credit(self):
        await self.tracker.ready({20: 40, 22: 40}, now=100.0)
        with patch("flock_cctv.collectors.time.time", return_value=110.0):
            await self.tracker.checkpoint()
        with patch.object(self.store, "companion_transition", new=AsyncMock(side_effect=OSError("disk"))):
            with self.assertRaises(OSError):
                await self.voice(120.0, member(22), in_channel(voice_channel(40, 20, 22)), NO_CHANNEL)
        self.assertFalse(self.tracker.collection_ready)
        await self.tracker.guild_available({20: 40}, now=140.0)
        self.assertEqual(roster(self.database, 20), [])
        self.assertEqual((await self.store.stats(20, "all", 150.0))["voice_seconds"], 20.0)

    async def test_pause_and_resume_after_voice_failure_reconcile_current_snapshot(self):
        await self.tracker.ready({20: 40, 21: 40}, now=100.0)
        with patch.object(self.store, "voice_transition", new=AsyncMock(side_effect=OSError("disk"))):
            with self.assertRaises(OSError):
                await self.voice(120.0, member(20), in_channel(voice_channel(40, 20, 21)), NO_CHANNEL)
        with patch("flock_cctv.collectors.time.time", return_value=130.0):
            await self.tracker.pause(99)
        self.assertFalse(await self.tracker.message(message(created=135.0)))
        with patch("flock_cctv.collectors.time.time", return_value=140.0):
            await self.tracker.resume(99, {21: 40})
        self.assertTrue(self.tracker.collection_ready)
        self.assertIsNone(self.tracker.last_error)
        self.assertIsNone(roster(self.database, 20))
        self.assertEqual(roster(self.database, 21), [])
        self.assertEqual((await self.store.stats(20, "all", 150.0))["voice_seconds"], 0.0)
        self.assertEqual((await self.store.stats(21, "all", 150.0))["voice_seconds"], 10.0)

    async def test_two_tracked_people_in_one_channel_are_each_others_companions(self):
        await self.tracker.ready({}, now=100.0)
        channel = voice_channel(40, 20)
        await self.voice(110.0, member(20), NO_CHANNEL, in_channel(channel))
        self.assertEqual(roster(self.database, 20), [])
        channel = voice_channel(40, 20, 21)
        await self.voice(120.0, member(21), NO_CHANNEL, in_channel(channel))
        self.assertEqual(roster(self.database, 20), [21])
        self.assertEqual(roster(self.database, 21), [20])

        # An untracked person joining updates both tracked rosters.
        channel = voice_channel(40, 20, 21, 22)
        await self.voice(130.0, member(22), NO_CHANNEL, in_channel(channel))
        self.assertEqual(roster(self.database, 20), [21, 22])
        self.assertEqual(roster(self.database, 21), [20, 22])

        # And leaving removes them again; the other tracked person leaving too.
        await self.voice(140.0, member(22), in_channel(channel), NO_CHANNEL)
        self.assertEqual(roster(self.database, 20), [21])
        await self.voice(150.0, member(21), in_channel(channel), NO_CHANNEL)
        self.assertEqual(roster(self.database, 20), [])
        self.assertIsNone(roster(self.database, 21))

        # Each person's company time reflects their own roster history.
        totals20 = {
            row["member_id"]: row["full_seconds"]
            for row in await self.store.company_totals(20, "all", 150.0)
        }
        self.assertAlmostEqual(totals20[0], 10)  # alone from 110-120
        self.assertAlmostEqual(totals20[21], 30)  # 120-150
        self.assertAlmostEqual(totals20[22], 10)  # 130-140
        totals21 = {
            row["member_id"]: row["full_seconds"]
            for row in await self.store.company_totals(21, "all", 150.0)
        }
        self.assertAlmostEqual(totals21[20], 30)
        self.assertAlmostEqual(totals21[22], 10)
        self.assertNotIn(0, totals21)

    async def test_tracked_member_moving_channels_updates_both_channels(self):
        await self.tracker.ready({}, now=100.0)
        first = voice_channel(40, 20, 21)
        await self.voice(110.0, member(20), NO_CHANNEL, in_channel(voice_channel(40, 20)))
        await self.voice(115.0, member(21), NO_CHANNEL, in_channel(first))
        self.assertEqual(roster(self.database, 21), [20])
        second = voice_channel(41, 20)
        await self.voice(
            120.0, member(20), in_channel(voice_channel(40, 21)), in_channel(second)
        )
        # 21 lost 20 from channel 40; 20 is now alone in channel 41.
        self.assertEqual(roster(self.database, 21), [])
        self.assertEqual(roster(self.database, 20), [])
        stats = await self.store.stats(20, "all", 120.0)
        self.assertEqual(stats["voice_visits"], 1)  # Moving keeps one visit.
        # 21 joins 41 later: 20's roster gains 21.
        await self.voice(
            130.0, member(21), in_channel(voice_channel(40, 21)),
            in_channel(voice_channel(41, 20, 21)),
        )
        self.assertEqual(roster(self.database, 20), [21])
        self.assertEqual(roster(self.database, 21), [20])

    async def test_ready_starts_incomplete_visits_with_correct_companions(self):
        await self.tracker.ready({20: 40, 21: 40, 22: 40, 23: 41}, now=200.0)
        self.assertEqual(roster(self.database, 20), [21, 22])
        self.assertEqual(roster(self.database, 21), [20, 22])
        # Visits are incomplete at the start: not counted as complete visits.
        for user in (20, 21):
            stats = await self.store.stats(user, "all", 200.0)
            self.assertEqual(stats["voice_visits"], 0)

    async def test_track_user_mid_call_with_real_store(self):
        await self.tracker.ready({}, now=100.0)
        with patch("flock_cctv.collectors.time.time", return_value=200.0):
            self.assertTrue(await self.tracker.track_user(22, 99, {20: 40, 22: 40}))
        self.assertEqual(roster(self.database, 22), [20])
        self.assertEqual(self.tracker.tracked_ids, frozenset({20, 21, 22}))
        with patch("flock_cctv.collectors.time.time", return_value=230.0):
            self.assertTrue(await self.tracker.untrack_user(22, 99))
        self.assertIsNone(roster(self.database, 22))
        self.assertEqual(self.tracker.tracked_ids, frozenset({20, 21}))
        # History is kept; the visit ended incomplete.
        self.assertGreater((await self.store.stats(22, "all", 230.0))["voice_seconds"], 0)

    async def test_per_person_message_and_voice_isolation(self):
        await self.tracker.ready({}, now=100.0)
        self.assertTrue(await self.tracker.message(message(message_id=1, user_id=20, created=150.0)))
        self.assertTrue(await self.tracker.message(message(message_id=2, user_id=20, created=151.0)))
        self.assertTrue(await self.tracker.message(message(message_id=3, user_id=21, created=152.0)))
        self.assertFalse(await self.tracker.message(message(message_id=4, user_id=22, created=153.0)))
        await self.voice(160.0, member(20), NO_CHANNEL, in_channel(voice_channel(40, 20)))
        await self.voice(190.0, member(20), in_channel(voice_channel(40, 20)), NO_CHANNEL)
        stats20 = await self.store.stats(20, "all", 200.0)
        stats21 = await self.store.stats(21, "all", 200.0)
        self.assertEqual((stats20["messages"], stats21["messages"]), (2, 1))
        self.assertAlmostEqual(stats20["voice_seconds"], 30)
        self.assertEqual(stats21["voice_seconds"], 0)
        self.assertIsNone(await self.store.last_voice(21, 200.0))
        self.assertEqual((await self.store.stats(22, "all", 200.0))["messages"], 0)

    async def test_checkpoint_and_shutdown_handle_several_open_visits(self):
        await self.tracker.ready({20: 40, 21: 41}, now=200.0)
        with patch("flock_cctv.collectors.time.time", return_value=260.0):
            await self.tracker.checkpoint()
        with patch("flock_cctv.collectors.time.time", return_value=270.0):
            await self.tracker.shutdown()
        # Shutdown closed the Store; reopen to inspect what was persisted.
        self.store = Store(self.database, self.root / "backups", "UTC")
        await self.store.initialize(300.0, self.config.guild_id)
        for user in (20, 21):
            stats = await self.store.stats(user, "all", 300.0, include_live=False)
            self.assertAlmostEqual(stats["voice_seconds"], 70)


if __name__ == "__main__":
    unittest.main()
