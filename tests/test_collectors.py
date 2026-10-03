from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flock_cctv.collectors import Tracker
from flock_cctv.config import Config
from flock_cctv.storage import Store


class FakeStore:
    def __init__(self, *, paused: bool = False, paused_by: int | None = None) -> None:
        self.paused = paused
        self.paused_by = paused_by
        self.tracking_since = 100.0
        self.last_checkpoint: float | None = None
        self.messages: set[int] = set()
        self.transitions: list[tuple[int | None, float, bool]] = []
        self.company_transitions: list[tuple[int, int, bool, float]] = []
        self.companions: frozenset[int] = frozenset()
        self.disconnections: list[float] = []
        self.checkpoints: list[float] = []
        self.connections: list[float] = []
        self.deleted = False
        self.closed = False

    async def state(self):
        return {
            "paused": self.paused,
            "paused_by": str(self.paused_by) if self.paused_by is not None else None,
            "tracking_since": self.tracking_since,
            "last_checkpoint": self.last_checkpoint,
        }

    async def connect(self, now):
        if not self.paused:
            self.connections.append(now)

    async def disconnect(self, now):
        self.disconnections.append(now)

    async def voice_transition(self, channel_id, now, complete_start=True, companions=frozenset()):
        self.transitions.append((channel_id, now, complete_start))
        self.companions = companions

    async def companion_transition(self, channel_id, member_id, joined, now):
        self.company_transitions.append((channel_id, member_id, joined, now))

    async def add_message(self, message_id, channel_id, created_at):
        if self.paused or message_id in self.messages:
            return False
        self.messages.add(message_id)
        return True

    async def checkpoint(self, now):
        self.checkpoints.append(now)
        self.last_checkpoint = now

    async def set_paused(self, paused, actor_id, now):
        self.paused = paused
        self.paused_by = actor_id if paused else None
        if paused:
            self.transitions.append((None, now, True))

    async def delete_data(self, actor_id, now):
        self.deleted = True
        self.paused = True
        self.paused_by = actor_id
        self.messages.clear()

    async def close(self):
        self.closed = True


class RecoveringStore(FakeStore):
    """Model an open SQLite interval that survives a failed disconnect call."""

    def __init__(self):
        super().__init__()
        self.active_channel: int | None = None
        self.store_connected = False
        self.fail_next_disconnect = False
        self.closed_voice: list[tuple[int, float]] = []
        self.operations: list[str] = []

    async def connect(self, now):
        self.operations.append("connect")
        if not self.paused:
            self.connections.append(now)
            self.store_connected = True

    async def disconnect(self, now):
        self.operations.append("disconnect")
        if self.fail_next_disconnect:
            self.fail_next_disconnect = False
            raise OSError("temporary storage failure")
        self.disconnections.append(now)
        if self.active_channel is not None:
            self.closed_voice.append((self.active_channel, self.last_checkpoint or now))
            self.transitions.append((None, self.last_checkpoint or now, False))
            self.active_channel = None
        self.store_connected = False

    async def voice_transition(self, channel_id, now, complete_start=True, companions=frozenset()):
        self.operations.append("voice")
        if self.active_channel == channel_id:
            return
        self.transitions.append((channel_id, now, complete_start))
        self.companions = companions
        self.active_channel = channel_id

    async def checkpoint(self, now):
        self.operations.append("checkpoint")
        await super().checkpoint(now)


def make_config(
    *,
    text_channel_ids: frozenset[int] | None = None,
    voice_channel_ids: frozenset[int] | None = None,
) -> Config:
    return Config(
        token="hidden-token",
        guild_id=10,
        target_user_id=20,
        owner_user_id=99,
        output_channel_id=None,
        text_channel_ids=text_channel_ids,
        voice_channel_ids=voice_channel_ids,
        timezone="UTC",
        database_path=Path("/tmp/test-tracker.sqlite3"),
        backup_dir=Path("/tmp/test-tracker-backups"),
    )


def message(*, message_id=101, guild_id=10, user_id=20, channel_id=30, created=200.0, bot=False):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=guild_id),
        author=SimpleNamespace(id=user_id, bot=bot),
        channel=SimpleNamespace(id=channel_id),
        created_at=datetime.fromtimestamp(created, tz=timezone.utc),
    )


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_resume_continues_visit_with_real_store(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config()
            store = Store(root / "tracker.sqlite3", root / "backups", "UTC")
            await store.initialize(100.0, config.guild_id, config.target_user_id)
            tracker = Tracker(config, store)
            try:
                await tracker.ready(None, now=100.0)
                await store.voice_transition(500, 110.0)
                with patch("flock_cctv.collectors.time.time", return_value=170.0):
                    await tracker.checkpoint()
                await tracker.disconnected(now=175.0)
                await tracker.ready(500, now=200.0)
                await store.voice_transition(None, 300.0)
                record = await store.records(300.0)
                self.assertAlmostEqual(record["longest_visit_seconds"], 160)
                self.assertEqual(record["longest_visit_at"], 110.0)
                self.assertEqual((await store.stats("all", 300.0))["voice_visits"], 1)
            finally:
                await store.close()

    async def test_message_filter_deduplicates_and_rejects_old_events(self):
        store = FakeStore()
        tracker = Tracker(make_config(text_channel_ids=frozenset({30})), store)
        await tracker.ready(None, now=150.0)

        self.assertTrue(await tracker.message(message(created=151.0)))
        self.assertFalse(await tracker.message(message(created=151.0)))
        self.assertFalse(await tracker.message(message(message_id=102, user_id=21)))
        self.assertFalse(await tracker.message(message(message_id=103, guild_id=11)))
        self.assertFalse(await tracker.message(message(message_id=104, channel_id=31)))
        self.assertFalse(await tracker.message(message(message_id=105, created=149.0)))
        self.assertFalse(await tracker.message(message(message_id=106, bot=True)))
        self.assertEqual(store.messages, {101})

    async def test_disconnect_and_guild_unavailable_stop_collection_until_reconciled(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready(40, now=150.0)
        self.assertTrue(tracker.connected)
        self.assertTrue(tracker.guild_is_available)
        self.assertEqual(store.transitions, [(40, 150.0, False)])

        await tracker.guild_unavailable(now=175.0)
        self.assertTrue(tracker.connected)
        self.assertFalse(tracker.guild_is_available)
        before = len(store.transitions)
        await tracker.voice(
            SimpleNamespace(id=20, guild=SimpleNamespace(id=10, afk_channel=None)),
            SimpleNamespace(channel=None),
            SimpleNamespace(channel=SimpleNamespace(id=41, guild=SimpleNamespace(id=10))),
        )
        self.assertEqual(len(store.transitions), before)

        await tracker.guild_available(41, now=200.0)
        self.assertTrue(tracker.guild_is_available)
        self.assertEqual(store.transitions[-1], (41, 200.0, False))

        await tracker.disconnected(now=210.0)
        self.assertFalse(tracker.connected)
        self.assertFalse(tracker.guild_is_available)
        self.assertFalse(await tracker.message(message(message_id=107, created=220.0)))
        await tracker.ready(None, now=300.0)
        self.assertTrue(tracker.connected)
        self.assertTrue(tracker.guild_is_available)

    async def test_reconnect_retries_failed_disconnect_before_opening_coverage(self):
        store = RecoveringStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready(40, now=150.0)
        with patch("flock_cctv.collectors.time.time", return_value=170.0):
            await tracker.checkpoint()

        store.fail_next_disconnect = True
        with self.assertRaises(OSError):
            await tracker.disconnected(now=200.0)
        self.assertEqual(store.active_channel, 40)

        await tracker.ready(40, now=300.0)
        self.assertEqual(store.closed_voice, [(40, 170.0)])
        self.assertEqual(store.active_channel, 40)
        self.assertEqual(store.transitions[-1], (40, 300.0, False))
        self.assertEqual(store.operations[-3:], ["disconnect", "connect", "voice"])

    async def test_voice_filters_target_guild_allowlist_and_afk_channel(self):
        store = FakeStore()
        tracker = Tracker(make_config(voice_channel_ids=frozenset({40, 41})), store)
        await tracker.ready(None, now=150.0)
        member = SimpleNamespace(
            id=20,
            guild=SimpleNamespace(id=10, afk_channel=SimpleNamespace(id=99)),
        )

        with patch("flock_cctv.collectors.time.time", return_value=160.0):
            await tracker.voice(
                member,
                SimpleNamespace(channel=None),
                SimpleNamespace(channel=SimpleNamespace(id=40, guild=SimpleNamespace(id=10))),
            )
            await tracker.voice(
                member,
                SimpleNamespace(channel=SimpleNamespace(id=40, guild=SimpleNamespace(id=10))),
                SimpleNamespace(channel=SimpleNamespace(id=41, guild=SimpleNamespace(id=10))),
            )
            await tracker.voice(
                member,
                SimpleNamespace(channel=SimpleNamespace(id=41, guild=SimpleNamespace(id=10))),
                SimpleNamespace(channel=SimpleNamespace(id=99, guild=SimpleNamespace(id=10))),
            )
            await tracker.voice(
                SimpleNamespace(id=21, guild=member.guild),
                SimpleNamespace(channel=None),
                SimpleNamespace(channel=SimpleNamespace(id=40, guild=member.guild)),
            )
        self.assertEqual(
            store.transitions,
            [(40, 160.0, True), (41, 160.0, True), (None, 160.0, True)],
        )

    async def test_peer_events_update_roster_only_for_tracked_target_channel(self):
        store = FakeStore()
        tracker = Tracker(make_config(voice_channel_ids=frozenset({40})), store)
        await tracker.ready(40, now=150, companions=frozenset({21}))
        self.assertEqual(store.companions, frozenset({21}))
        guild = SimpleNamespace(id=10, afk_channel=None)
        peer = SimpleNamespace(id=22, guild=guild, bot=False)
        channel = SimpleNamespace(id=40, guild=guild)
        with patch("flock_cctv.collectors.time.time", return_value=160):
            await tracker.voice(peer, SimpleNamespace(channel=None), SimpleNamespace(channel=channel))
            await tracker.voice(peer, SimpleNamespace(channel=channel), SimpleNamespace(channel=None))
            await tracker.voice(
                SimpleNamespace(id=23, guild=guild, bot=True),
                SimpleNamespace(channel=None), SimpleNamespace(channel=channel),
            )
        self.assertEqual(store.company_transitions, [(40, 22, True, 160), (40, 22, False, 160)])

    async def test_failed_reconciliation_blocks_writes_until_retry_succeeds(self):
        store = RecoveringStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready(40, now=150)
        with patch("flock_cctv.collectors.time.time", return_value=170):
            await tracker.checkpoint()
        store.fail_next_disconnect = True
        with self.assertRaises(OSError):
            await tracker.disconnected(now=200)
        store.fail_next_disconnect = True
        with self.assertRaises(OSError):
            await tracker.ready(40, now=300)
        self.assertFalse(tracker.collection_ready)
        self.assertFalse(await tracker.message(message(created=310)))
        before_operations = list(store.operations)
        await tracker.checkpoint()
        await tracker.voice(
            SimpleNamespace(id=20, guild=SimpleNamespace(id=10, afk_channel=None)),
            SimpleNamespace(channel=SimpleNamespace(id=40)),
            SimpleNamespace(channel=None),
        )
        self.assertEqual(store.operations, before_operations)
        await tracker.guild_available(40, now=400)
        self.assertTrue(tracker.collection_ready)
        self.assertEqual(store.closed_voice, [(40, 170)])
        self.assertTrue(await tracker.message(message(created=410)))

    async def test_shutdown_does_not_checkpoint_failed_reconciliation(self):
        store = RecoveringStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready(40, now=150)
        with patch("flock_cctv.collectors.time.time", return_value=170):
            await tracker.checkpoint()
        store.fail_next_disconnect = True
        with self.assertRaises(OSError):
            await tracker.ready(40, now=300)
        with patch("flock_cctv.collectors.time.time", return_value=400):
            await tracker.shutdown()
        self.assertEqual(store.checkpoints, [170])
        self.assertEqual(store.closed_voice, [(40, 170)])

    async def test_admin_can_resume_legacy_target_pause(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready(None, now=150)
        await tracker.pause(99)
        await tracker.pause(20)
        await tracker.resume(99, None)
        self.assertFalse(store.paused)

    async def test_admin_resume_reconciles_incomplete_visit(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready(None, now=150.0)
        with patch("flock_cctv.collectors.time.time", return_value=160.0):
            await tracker.pause(20)
        self.assertTrue(store.paused)
        await tracker.disconnected(now=170.0)
        with patch("flock_cctv.collectors.time.time", return_value=200.0):
            await tracker.resume(99, 40)
        self.assertFalse(store.paused)
        self.assertFalse(tracker.guild_is_available)
        await tracker.gateway_ready()
        await tracker.guild_available(40, now=210.0)
        self.assertEqual(store.transitions[-1], (40, 210.0, False))

    async def test_shutdown_truncates_active_visit_and_checkpoint_does_not_clear_other_health_error(self):
        store = FakeStore()
        tracker = Tracker(make_config(), store)
        await tracker.ready(40, now=150.0)
        tracker.report_error("maintenance", OSError("disk failure"))
        with patch("flock_cctv.collectors.time.time", return_value=180.0):
            await tracker.shutdown()
        self.assertEqual(store.checkpoints, [180.0])
        self.assertEqual(store.disconnections, [150.0, 180.0])
        self.assertEqual(store.transitions, [(40, 150.0, False)])
        self.assertTrue(store.closed)
        self.assertEqual(tracker.last_error, "maintenance failed (OSError)")


if __name__ == "__main__":
    unittest.main()
