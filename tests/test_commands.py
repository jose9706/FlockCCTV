import asyncio
from io import BytesIO
import inspect
import re
import unittest
from unittest.mock import AsyncMock, call, patch
from types import SimpleNamespace

import flock_cctv
import discord
from PIL import Image

from flock_cctv import commands as commands_module
from flock_cctv.jokes import SharedRoastCooldown
from flock_cctv.commands import (
    DeleteDataConfirmation,
    _can_control,
    _company_report,
    _leaderboard_text,
    _online_text,
    _pie_png,
    _records_text,
    _report_is_ephemeral,
    _execute_after_scope,
    _scope_ok,
    _stats_text,
    _bursts,
    _ghost_days,
    _streaks,
    _trend_report,
    _status_text,
    _seen_text,
    register_commands,
)


class FakeTree:
    def __init__(self):
        self.commands = []

    def add_command(self, command, *, guild):
        self.commands.append((command, guild))


class FakeResponse:
    def __init__(self):
        self.done = False
        self.sent = []
        self.deferred = False

    def is_done(self):
        return self.done

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        self.done = True

    async def defer(self, **kwargs):
        self.deferred = True
        self.done = True

    async def edit_message(self, **kwargs):
        self.sent.append(kwargs)
        self.done = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class FakeInteraction:
    def __init__(self, *, guild_id=10, channel_id=20, user=None):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user = user or SimpleNamespace(id=30, guild_permissions=SimpleNamespace(manage_guild=False))
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edited = []

    async def edit_original_response(self, **kwargs):
        self.edited.append(kwargs)


def _row(user_id, *, active=True, since=1_690_000_000.0, updated=1_700_000_000.0):
    return {
        "user_id": user_id, "active": active, "tracking_since": since,
        "added_by": 31, "updated_at": updated,
    }


def _rank(user_id, messages=0, voice=0.0, visits=0, days=0, tracked=True):
    return {
        "user_id": user_id, "messages": messages, "voice_seconds": voice,
        "voice_visits": visits, "active_days": days, "tracked": tracked,
    }


class FakeStore:
    def __init__(self):
        self.deleted = []
        # Every per-person read records the user ID it was asked about.
        self.user_ids = []
        self.tracked = [_row(30), _row(41), _row(42, active=False)]
        self.ranking_rows = []
        self.ranking_calls = []
        self.last_voice_result = None
        self.company_rows = []
        self.trend_rows = []
        self.company_rows_daily = []
        self.message_times_result = {"times": [], "since": 0.0, "period_start": 0.0}
        self.voice_hours_result = {"hours": [0.0] * 24, "since": 0.0, "period_start": 0.0}
        self.comparison = {"current": {}, "previous": None, "reason": "all"}
        self.admin_decisions = {}
        self.paused = False

    async def tracked_users(self):
        return [dict(row) for row in self.tracked]

    async def coverage_gap_seconds(self, now):
        return 125.0

    async def ranking(self, period, now, *, include_live=True):
        self.include_live = include_live
        self.ranking_calls.append(period)
        return list(self.ranking_rows)

    async def admin_override(self, user_id):
        return self.admin_decisions.get(user_id)

    async def admin_overrides(self):
        return dict(self.admin_decisions)

    async def set_admin_override(self, user_id, enabled):
        self.admin_decisions[user_id] = enabled

    async def company_totals(self, user_id, period, now, *, include_live=True):
        self.user_ids.append(user_id)
        self.include_live = include_live
        return self.company_rows

    async def daily_trend(self, user_id, period, now, *, include_live=True):
        self.user_ids.append(user_id)
        self.include_live = include_live
        self.trend_period = period
        return self.trend_rows

    async def message_times(self, user_id, period, now):
        self.user_ids.append(user_id)
        return self.message_times_result

    async def voice_hours(self, user_id, period, now, *, include_live=True):
        self.user_ids.append(user_id)
        return self.voice_hours_result

    async def period_comparison(self, user_id, period, now, *, include_live=True):
        self.user_ids.append(user_id)
        return self.comparison

    async def company_daily(self, user_id, period, now, *, include_live=True):
        self.user_ids.append(user_id)
        return self.company_rows_daily

    async def last_voice(self, user_id, now, *, include_live=True):
        self.user_ids.append(user_id)
        self.include_live = include_live
        return self.last_voice_result

    async def stats(self, user_id, period, now, *, include_live=True):
        self.user_ids.append(user_id)
        self.include_live = include_live
        return {
            "messages": 4,
            "voice_seconds": 62,
            "voice_visits": 1,
            "active_days": 2,
            "tracking_since": 1_700_000_000,
            "gap_seconds": 90,
            "paused": False,
            "secret_channel_name": "private-channel-marker",
        }

    async def records(self, user_id, now, *, include_live=True):
        self.user_ids.append(user_id)
        self.include_live = include_live
        return {}

    async def state(self):
        return {
            "paused": self.paused, "evil_mode": False, "reaction_mode": False, "paused_by": None,
            "tracking_since": 1_700_000_000, "last_checkpoint": 1_700_000_000,
        }


# Leland (the requester in most tests) is tracked and has been since before the database began.
PERSON = commands_module._Person(30, "Leland", True, 1_690_000_000.0)


def _user(user_id, name, *, bot=False, username=None):
    return SimpleNamespace(id=user_id, display_name=name, name=username or name.lower(), bot=bot)


def _fake_guild(bot, members=None):
    """Install a guild whose members come from ``members``; nobody else can be fetched."""
    members = dict(members or {})
    guild = SimpleNamespace(
        default_role=object(),
        get_member=members.get,
        fetch_member=AsyncMock(side_effect=TimeoutError),
    )
    bot.get_guild = lambda guild_id: guild
    bot.get_user = lambda user_id: None
    bot.fetch_user = AsyncMock(side_effect=TimeoutError)
    return guild


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            guild_id=10,
            output_channel_id=None,
            public_report_channel_ids=frozenset(),
            leland_user_id=30,
            owner_user_id=31,
            admin_user_ids=frozenset({33}),
            timezone="America/Costa_Rica",
        )
        self.bot = SimpleNamespace(config=self.config, tree=FakeTree(), store=FakeStore())
        self.bot.tracker = SimpleNamespace(connected=True, guild_is_available=True, last_error=None)
        self.bot.get_channel = lambda channel_id: None
        self.bot.voice_snapshot = lambda: {}

    def group(self):
        register_commands(self.bot)
        return next(group for group, _ in self.bot.tree.commands if group.name == "flock")

    def command(self, name, subgroup=None):
        group = self.group()
        if subgroup is not None:
            group = next(command for command in group.commands if command.name == subgroup)
        return next(command for command in group.commands if command.name == name)

    def test_registers_single_guild_scoped_group_and_default_week(self):
        register_commands(self.bot)
        commands = {group.name: (group, guild) for group, guild in self.bot.tree.commands}
        self.assertEqual(set(commands), {"flock"})
        for group, guild in commands.values():
            self.assertEqual(guild.id, self.config.guild_id)
        flock = commands["flock"][0]
        self.assertEqual(
            {command.name for command in flock.commands},
            {
                "stats", "records", "where", "company", "leaderboard", "trends", "online", "roast",
                "top", "help", "about", "version", "update", "pause", "resume", "delete-data",
                "evil-mode", "reaction-mode", "admin", "track",
            },
        )
        admin = next(command for command in flock.commands if command.name == "admin")
        self.assertEqual({command.name for command in admin.commands}, {"add", "remove", "list"})
        track = next(command for command in flock.commands if command.name == "track")
        self.assertEqual({command.name for command in track.commands}, {"add", "remove", "list"})
        stats = next(command for command in flock.commands if command.name == "stats")
        roast = next(command for command in flock.commands if command.name == "roast")
        self.assertEqual(inspect.signature(stats.callback).parameters["period"].default, "week")
        self.assertEqual(inspect.signature(roast.callback).parameters["period"].default, "week")
        trends = next(command for command in flock.commands if command.name == "trends")
        self.assertEqual(inspect.signature(trends.callback).parameters["period"].default, "last7")
        self.assertEqual(inspect.signature(trends.callback).parameters["kind"].default, "daily")
        company = next(command for command in flock.commands if command.name == "company")
        for command in (company, trends):
            with self.subTest(command=command.name):
                self.assertEqual(inspect.signature(command.callback).parameters["count"].default, "split")
                count = next(item for item in command.parameters if item.name == "count")
                self.assertFalse(count.required)
                self.assertEqual([choice.value for choice in count.choices], ["split", "full"])
        leaderboard = next(command for command in flock.commands if command.name == "leaderboard")
        self.assertEqual(inspect.signature(leaderboard.callback).parameters["period"].default, "all")
        top = next(command for command in flock.commands if command.name == "top")
        self.assertEqual(inspect.signature(top.callback).parameters["period"].default, "week")
        self.assertEqual(inspect.signature(top.callback).parameters["metric"].default, "messages")
        metric = next(item for item in top.parameters if item.name == "metric")
        self.assertEqual([choice.value for choice in metric.choices], ["messages", "voice", "active_days"])

    def test_per_person_commands_take_an_optional_user_defaulting_to_the_requester(self):
        flock = self.group()
        for name in ("stats", "records", "where", "company", "leaderboard", "trends", "online", "roast"):
            with self.subTest(command=name):
                command = next(command for command in flock.commands if command.name == name)
                parameter = next(item for item in command.parameters if item.name == "user")
                self.assertFalse(parameter.required)
                self.assertEqual(parameter.type, discord.AppCommandOptionType.user)
                self.assertEqual(parameter.description, "Whose activity to show (defaults to you)")
                self.assertIsNone(inspect.signature(command.callback).parameters["user"].default)
        for name in ("top", "help", "about", "version", "update", "pause", "resume"):
            with self.subTest(command=name):
                command = next(command for command in flock.commands if command.name == name)
                self.assertNotIn("user", [item.name for item in command.parameters])
        delete = next(command for command in flock.commands if command.name == "delete-data")
        self.assertFalse(next(item for item in delete.parameters if item.name == "user").required)
        add = self.command("add", "track")
        self.assertTrue(next(item for item in add.parameters if item.name == "user").required)
        remove = self.command("remove", "track")
        self.assertTrue(next(item for item in remove.parameters if item.name == "user_id").required)

    async def test_records_text_formats_dates_and_shows_live_visit(self):
        async def records(user_id, now, *, include_live=True):
            return {
                "busiest_day": "2026-09-29",
                "busiest_day_messages": 7,
                "longest_visit_seconds": 5400.0,
                "longest_visit_at": 1_790_000_000.0,
                "current_visit_seconds": 600.0,
                "current_visit_complete_start": False,
            }

        guild = SimpleNamespace(
            default_role=object(),
            get_member=lambda user_id: SimpleNamespace(display_name={41: "Alice", 42: "Bob"}.get(user_id)),
        )
        self.bot.get_guild = lambda guild_id: guild
        visible = SimpleNamespace(permissions_for=lambda viewer: SimpleNamespace(view_channel=viewer is guild.default_role))
        hidden = SimpleNamespace(permissions_for=lambda viewer: SimpleNamespace(view_channel=False))
        self.bot.get_channel = lambda channel_id: {101: visible, 102: hidden}.get(channel_id)
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.company_rows = [
            {"channel_id": 101, "member_id": 0, "seconds": 9000.0},
            {"channel_id": 101, "member_id": 41, "seconds": 3000.0},
            {"channel_id": 101, "member_id": 42, "seconds": 600.0},
            {"channel_id": 102, "member_id": 42, "seconds": 9000.0},
        ]
        interaction = FakeInteraction(channel_id=20)
        interaction.guild = guild
        self.bot.store.records = records
        content = await _records_text(self.bot, interaction, PERSON)
        self.assertIn("Most messages in one day: **7** on Tue Sep 29, 2026.", content)
        self.assertIn("Longest fully observed voice visit: **1h 30m**, started ", content)
        self.assertIn(
            "Current voice visit so far: **10m** (its start was not observed, "
            "so it cannot set the record).",
            content,
        )
        # Alone time is not a companion, and Bob's hidden-channel time is excluded.
        self.assertIn(
            "Top voice companion: **Alice** — 50m of shared voice time since companion tracking began",
            content,
        )

    async def test_records_text_without_data(self):
        content = await _records_text(self.bot, FakeInteraction(), PERSON)
        self.assertIn("No personal records have been recorded yet.", content)
        self.assertNotIn("Top voice companion", content)
        self.assertTrue(self.bot.store.include_live)

    async def test_records_text_live_visit_alone_and_unreliable_collection(self):
        async def records(user_id, now, *, include_live=True):
            self.bot.store.include_live = include_live
            return {"current_visit_seconds": 300.0, "current_visit_complete_start": True}

        self.bot.store.records = records
        content = await _records_text(self.bot, FakeInteraction(), PERSON)
        lines = content.splitlines()
        self.assertEqual(lines[-2], "No personal records have been recorded yet.")
        self.assertEqual(
            lines[-1],
            "Current voice visit so far: **5m** (it can set the record once it ends).",
        )
        self.bot.tracker.connected = False
        await _records_text(self.bot, FakeInteraction(), PERSON)
        self.assertFalse(self.bot.store.include_live)

    def test_company_pie_contains_large_visible_legend_text(self):
        png = _pie_png([("Alice 🎮", 90), ("Alone", 30)], "Leland")
        with Image.open(BytesIO(png)) as chart:
            self.assertEqual(chart.size, (1100, 640))
            label_region = chart.crop((660, 89, 1058, 125))
            dark_pixels = sum(
                max(pixel) < 100 for pixel in label_region.getdata()
            )
            self.assertGreater(dark_pixels, 100)

    def test_streaks_keep_current_run_alive_until_today_ends(self):
        def day(messages, voice=0.0):
            return {"messages": messages, "voice_seconds": voice}

        self.assertEqual(_streaks([day(1), day(1), day(0), day(0, 5), day(2), day(0)]), (2, 2))
        self.assertEqual(_streaks([day(1), day(0), day(0)]), (1, 0))
        self.assertEqual(_streaks([]), (0, 0))

    def test_ghost_days_need_full_watch_and_unwatched_days_break_runs(self):
        def day(name, messages, watched):
            return {"day": name, "messages": messages, "voice_seconds": 0.0, "watched": watched}

        series = [
            day("2025-03-01", 0, True), day("2025-03-02", 0, True), day("2025-03-03", 0, False),
            day("2025-03-04", 0, True), day("2025-03-05", 3, True), day("2025-03-06", 0, False),
        ]
        self.assertEqual(_ghost_days(series), (3, 2, "2025-03-01", "2025-03-02"))
        self.assertEqual(_ghost_days([day("2025-03-01", 0, False)]), (0, 0, None, None))

    def test_bursts_split_on_two_minute_quiet_gaps(self):
        self.assertEqual(
            _bursts([0.0, 60.0, 180.0, 400.0, 401.0]),
            [(0.0, 180.0, 3), (400.0, 401.0, 2)],
        )
        self.assertEqual(_bursts([]), [])

    async def test_daily_trend_reports_busiest_day_streaks_ghosts_and_chart(self):
        self.bot.store.trend_rows = [
            {"day": "2025-03-02", "messages": 0, "voice_seconds": 0.0, "voice_visits": 0, "watched": True},
            {"day": "2025-03-03", "messages": 4, "voice_seconds": 60.0, "voice_visits": 1, "watched": True},
            {"day": "2025-03-04", "messages": 9, "voice_seconds": 0.0, "voice_visits": 0, "watched": True},
            {"day": "2025-03-05", "messages": 0, "voice_seconds": 0.0, "voice_visits": 0, "watched": False},
        ]
        content, png = await _trend_report(self.bot, FakeInteraction(), PERSON, "week", "daily")
        self.assertIn("Busiest day: **Tue Mar 4** — 9 messages", content)
        self.assertIn("Active days: **2** of 3 observed", content)
        self.assertIn("Longest active streak: **2 days**", content)
        self.assertIn("current streak: **2 days**", content)
        self.assertIn("Ghost days: **1**; longest ghost streak **1 day** (Sun Mar 2)", content)
        self.assertTrue(self.bot.store.include_live)
        with Image.open(BytesIO(png)) as chart:
            self.assertEqual(chart.size[0], commands_module._CHART_WIDTH)

    def test_axis_ticks_are_round_start_at_zero_and_cover_the_peak(self):
        ticks = commands_module._axis_ticks
        self.assertEqual(ticks(110, "count"), [0, 50, 100, 150])
        self.assertEqual(ticks(3, "count"), [0, 1, 2, 3])
        self.assertEqual(ticks(0, "count"), [0.0, 1.0])
        self.assertEqual(ticks(4.3, "average"), [0, 2, 4, 6])
        self.assertEqual(ticks(15000, "duration"), [0, 7200, 14400, 21600])
        self.assertEqual(ticks(2400, "duration"), [0, 600, 1200, 1800, 2400])
        self.assertEqual(ticks(0, "duration"), [0.0, 3600.0])
        for peak, kind in ((7, "count"), (0.37, "average"), (95_000, "duration"), (2_000_000, "duration")):
            values = ticks(peak, kind)
            self.assertEqual(values[0], 0)
            self.assertGreaterEqual(values[-1], peak)
            self.assertLessEqual(len(values), 5)

        label = commands_module._axis_label
        self.assertEqual(
            [label(value, "duration") for value in (0, 900, 3600, 5400)], ["0", "15m", "1h", "1h30"]
        )
        self.assertEqual([label(value, "average") for value in (0.5, 2.0)], ["0.5", "2"])
        self.assertEqual(label(1500, "count"), "1,500")

    def test_trend_chart_text_stays_legible_when_discord_shrinks_it(self):
        # Discord previews attachments around 400px wide; axis text must stay
        # at least ~12px there, so it has to be about 3% of the canvas width.
        _, axis_font = commands_module._chart_fonts()
        self.assertGreaterEqual(axis_font.size / commands_module._CHART_WIDTH, 0.03)
        long_title = "Leland's average day of the week — this month and then some more"
        png = commands_module._bar_panels_png(long_title, ["Mon"], [("Messages", [1.0], "#000000", "count")])
        with Image.open(BytesIO(png)) as chart:
            title_band = chart.convert("L").crop((0, 0, chart.width, 90))
            right_edge = title_band.crop((chart.width - 30, 0, chart.width, 90))
            self.assertEqual(min(right_edge.getdata()), 255)  # title fits inside the margin

    async def test_daily_trend_groups_long_periods_and_weekday_trend_averages(self):
        self.bot.store.trend_rows = [
            {"day": f"2025-{month:02d}-{day:02d}", "messages": day % 3, "voice_seconds": 0.0, "voice_visits": 0}
            for month in (1, 2, 3) for day in range(1, 29)
        ]
        content, png = await _trend_report(self.bot, FakeInteraction(), PERSON, "all", "daily")
        self.assertIn("groups days by week", content)
        self.assertIsNotNone(png)

        self.bot.store.trend_rows = [
            {"day": "2025-03-03", "messages": 2, "voice_seconds": 0.0, "voice_visits": 0},
            {"day": "2025-03-10", "messages": 6, "voice_seconds": 0.0, "voice_visits": 0},
            {"day": "2025-03-14", "messages": 3, "voice_seconds": 7200.0, "voice_visits": 1},
            # An unwatched quiet Monday is unknown and must not dilute the average.
            {"day": "2025-03-17", "messages": 0, "voice_seconds": 0.0, "voice_visits": 0, "watched": False},
        ]
        content, png = await _trend_report(self.bot, FakeInteraction(), PERSON, "month", "weekdays")
        self.assertIn("Chattiest day: **Monday** — 4.0 messages on average", content)
        self.assertIn("Most voice time: **Friday** — 2h 0m on average", content)
        self.assertIsNotNone(png)

    async def test_trends_without_activity_send_text_only(self):
        for kind, expected in (
            ("daily", "No activity has been recorded for this week"),
            ("hours", "No messages or voice time"),
            ("bursts", "No messages"),
            ("company", "No companion time"),
        ):
            content, png = await _trend_report(self.bot, FakeInteraction(), PERSON, "week", kind)
            self.assertIn(expected, content)
            self.assertIsNone(png)

        self.bot.store.comparison = {
            "current": {"start": 100.0, "end": 200.0, "messages": 3, "voice_seconds": 0.0,
                        "voice_visits": 0, "watched_seconds": 100.0},
            "previous": {"start": 0.0, "end": 100.0, "messages": 2, "voice_seconds": 0.0,
                         "voice_visits": 0, "watched_seconds": 50.0},
            "reason": None,
        }
        content, _ = await _trend_report(self.bot, FakeInteraction(), PERSON, "last7", "compare")
        self.assertIn("**Leland vs the previous 7 days — the last 7 days so far**", content)
        self.assertIn("watched 50% of the previous 7 days' window", content)

    async def test_hour_trend_reports_message_and_voice_peaks_and_retention(self):
        # 1_700_000_000 is 16:13 in Costa Rica; +10 hours is 02:13.
        base = 1_700_000_000.0
        self.bot.store.message_times_result = {
            "times": [base, base + 36_000, base + 36_010, base + 36_020],
            "since": base + 86_400, "period_start": base,
        }
        voice = [0.0] * 24
        voice[22] = 5400.0
        voice[1] = 1800.0
        self.bot.store.voice_hours_result = {"hours": voice, "since": base + 86_400, "period_start": base}
        content, png = await _trend_report(self.bot, FakeInteraction(), PERSON, "all", "hours")
        self.assertIn("Peak message hour: **02:00–03:00** with 3 of 4 messages", content)
        self.assertIn("Peak voice hour: **22:00–23:00** with 1h 30m of 2h 0m", content)
        self.assertIn("**75%** of messages and **25%** of voice time", content)
        self.assertIn("covers activity since", content)
        self.assertIsNotNone(png)

    async def test_compare_trend_reports_changes_watch_coverage_and_refusals(self):
        self.bot.store.comparison = {
            "current": {"start": 100.0, "end": 200.0, "messages": 30, "voice_seconds": 3600.0,
                        "voice_visits": 2, "watched_seconds": 100.0},
            "previous": {"start": 0.0, "end": 100.0, "messages": 20, "voice_seconds": 3600.0,
                         "voice_visits": 0, "watched_seconds": 50.0},
            "reason": None,
        }
        content, png = await _trend_report(self.bot, FakeInteraction(), PERSON, "week", "compare")
        self.assertIn("**Leland vs last week — this week so far**", content)
        self.assertIn("Messages: **30** vs 20 (up 50%)", content)
        self.assertIn("(no change)", content)
        self.assertIn("Voice visits: **2** vs 0 (new this period)", content)
        self.assertIn("watched 50% of last week's window and 100% of this one", content)
        self.assertIsNotNone(png)

        for reason, expected in (
            ("all", "All time has no previous period"),
            ("untracked", "Tracking began partway through last week"),
            ("pruned", "Last week is older than the retained detail"),
        ):
            self.bot.store.comparison = {"current": {}, "previous": None, "reason": reason}
            content, png = await _trend_report(self.bot, FakeInteraction(), PERSON, "week", "compare")
            self.assertIn(expected, content)
            self.assertIsNone(png)

    async def test_burst_trend_reports_biggest_average_and_rapid_share(self):
        base = 1_700_000_000.0
        times = [base + offset * 10 for offset in range(6)] + [base + 1000, base + 5000]
        self.bot.store.message_times_result = {"times": times, "since": base, "period_start": base}
        content, png = await _trend_report(self.bot, FakeInteraction(), PERSON, "month", "bursts")
        self.assertIn("Biggest burst: **6 messages** in 50s", content)
        self.assertIn("Average burst: **2.7 messages** across 3 bursts", content)
        self.assertIn("Rapid fire: **75%**", content)
        self.assertNotIn("covers activity since", content)
        self.assertIsNotNone(png)

    async def test_company_trend_filters_channels_and_names_top_companion_per_bucket(self):
        guild = SimpleNamespace(
            default_role=object(),
            get_member=lambda user_id: SimpleNamespace(display_name={41: "Alice", 42: "Bob"}.get(user_id)),
        )
        self.bot.get_guild = lambda guild_id: guild
        visible = SimpleNamespace(permissions_for=lambda viewer: SimpleNamespace(view_channel=viewer is guild.default_role))
        hidden = SimpleNamespace(permissions_for=lambda viewer: SimpleNamespace(view_channel=False))
        self.bot.get_channel = lambda channel_id: {101: visible, 102: hidden}.get(channel_id)
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.company_rows_daily = [
            {"day": "2025-03-03", "channel_id": 101, "member_id": 41, "seconds": 600.0},
            {"day": "2025-03-03", "channel_id": 101, "member_id": 42, "seconds": 60.0},
            {"day": "2025-03-04", "channel_id": 101, "member_id": 0, "seconds": 300.0},
            {"day": "2025-03-04", "channel_id": 102, "member_id": 43, "seconds": 9000.0},
        ]
        interaction = FakeInteraction(channel_id=20)
        interaction.guild = guild
        with patch.object(commands_module.time, "time", return_value=1_741_176_000.0):  # 2025-03-05 06:00 in Costa Rica
            content, png = await _trend_report(self.bot, interaction, PERSON, "week", "company")
        self.assertIn("Mon 3: **Alice** — 10m", content)
        self.assertIn("Tue 4: alone — 5m", content)
        self.assertIn("Wed 5: no company time", content)
        self.assertNotIn("43", content)
        self.assertIsNotNone(png)

    async def test_company_trend_names_bucket_winner_outside_overall_top_five(self):
        guild = SimpleNamespace(
            default_role=object(),
            get_member=lambda user_id: SimpleNamespace(display_name=f"Peer {user_id}"),
        )
        self.bot.get_guild = lambda guild_id: guild
        visible = SimpleNamespace(permissions_for=lambda viewer: SimpleNamespace(view_channel=True))
        self.bot.get_channel = lambda channel_id: visible
        rows = [
            {"day": "2025-03-03", "channel_id": 101, "member_id": member_id, "seconds": 3600.0}
            for member_id in range(41, 46)
        ]
        rows += [
            {"day": "2025-03-04", "channel_id": 101, "member_id": 41, "seconds": 60.0},
            {"day": "2025-03-04", "channel_id": 101, "member_id": 49, "seconds": 1200.0},
        ]
        self.bot.store.company_rows_daily = rows
        with patch.object(commands_module.time, "time", return_value=1_741_132_800.0):
            content, _ = await _trend_report(self.bot, FakeInteraction(), PERSON, "week", "company")
        self.assertIn("Tue 4: **Peer 49** — 20m", content)

    async def test_trends_command_attaches_chart_with_report_visibility(self):
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.trend_rows = [
            {"day": "2025-03-03", "messages": 4, "voice_seconds": 60.0, "voice_visits": 1},
        ]
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        trends = next(command for command in flock.commands if command.name == "trends")
        public = FakeInteraction(channel_id=20)
        await trends.callback(public)
        self.assertEqual(self.bot.store.trend_period, "last7")
        sent = public.followup.sent[0]
        self.assertFalse(sent["ephemeral"])
        self.assertEqual(sent["file"].filename, "flock-trends-daily.png")
        private = FakeInteraction(channel_id=21)
        await trends.callback(private, period="week", kind="weekdays")
        self.assertTrue(private.followup.sent[0]["ephemeral"])

    async def test_wrong_guild_and_wrong_output_channel_are_rejected_ephemerally(self):
        wrong_guild = FakeInteraction(guild_id=99)
        self.assertFalse(await _scope_ok(wrong_guild, self.bot))
        self.assertTrue(wrong_guild.response.sent[0]["ephemeral"])

        self.config.output_channel_id = 22
        wrong_channel = FakeInteraction(channel_id=20)
        self.assertFalse(await _scope_ok(wrong_channel, self.bot))
        self.assertTrue(wrong_channel.response.sent[0]["ephemeral"])
        self.assertIsInstance(wrong_channel.response.sent[0]["allowed_mentions"], discord.AllowedMentions)

    async def test_report_visibility_uses_invocation_channel(self):
        self.config.public_report_channel_ids = frozenset({20})
        self.assertFalse(_report_is_ephemeral(self.bot, FakeInteraction(channel_id=20)))
        self.assertTrue(_report_is_ephemeral(self.bot, FakeInteraction(channel_id=21)))
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        stats = next(command for command in flock.commands if command.name == "stats")
        where = next(command for command in flock.commands if command.name == "where")
        public = FakeInteraction(channel_id=20)
        await stats.callback(public)
        self.assertEqual(public.followup.sent[0]["ephemeral"], False)
        private = FakeInteraction(channel_id=21)
        await stats.callback(private)
        self.assertEqual(private.followup.sent[0]["ephemeral"], True)
        where_interaction = FakeInteraction(channel_id=20)
        await where.callback(where_interaction)
        self.assertFalse(where_interaction.followup.sent[0]["ephemeral"])
        private_where = FakeInteraction(channel_id=21)
        await where.callback(private_where)
        self.assertTrue(private_where.followup.sent[0]["ephemeral"])

    async def test_company_chart_filters_source_channels_for_public_and_private_reports(self):
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": 41, "seconds": 60},
            {"channel_id": 41, "member_id": 42, "seconds": 120},
        ]
        role = SimpleNamespace(id=0)
        self.bot.get_guild = lambda guild_id: SimpleNamespace(
            get_member=lambda member_id: SimpleNamespace(
                display_name="Public friend" if member_id == 41 else "Private friend"
            )
        )
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(
                view_channel=channel_id == 40 or viewer is not role
            )
        )
        public = FakeInteraction(channel_id=20)
        public.guild = SimpleNamespace(default_role=role)
        content, png = await _company_report(self.bot, public, PERSON, "week")
        self.assertIn("Public friend", content)
        self.assertNotIn("Private friend", content)
        self.assertIn("1m", content)
        self.assertTrue(png.startswith(b"\x89PNG"))

        private = FakeInteraction(channel_id=21)
        private.guild = SimpleNamespace(default_role=role)
        content, _ = await _company_report(self.bot, private, PERSON, "week")
        self.assertIn("Private friend", content)
        self.assertIn("3m", content)

        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": member_id, "seconds": 10}
            for member_id in range(50, 59)
        ] + [{"channel_id": 40, "member_id": 0, "seconds": 5}]
        content, _ = await _company_report(self.bot, public, PERSON, "week")
        self.assertIn("Alone", content)
        self.assertIn("Other people", content)

    async def test_company_chart_can_count_full_shared_time(self):
        self.config.public_report_channel_ids = frozenset({20})
        names = {41: "Alice", 42: "Bob"}
        self.bot.get_guild = lambda guild_id: SimpleNamespace(
            get_member=lambda member_id: SimpleNamespace(display_name=names.get(member_id, f"P{member_id}"))
        )
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(view_channel=True)
        )
        # A one-hour call with Alice and Bob, plus 30 minutes alone: 1h 30m observed.
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": 41, "seconds": 1800.0, "full_seconds": 3600.0},
            {"channel_id": 40, "member_id": 42, "seconds": 1800.0, "full_seconds": 3600.0},
            {"channel_id": 40, "member_id": 0, "seconds": 1800.0, "full_seconds": 1800.0},
        ]
        public = FakeInteraction(channel_id=20)
        public.guild = SimpleNamespace(default_role=object())

        content, _ = await _company_report(self.bot, public, PERSON, "week")
        self.assertIn("**Alice** — 30m (33.3%)", content)
        self.assertIn("split evenly", content)

        content, png = await _company_report(self.bot, public, PERSON, "week", "full")
        self.assertIn("(full time with each person)", content)
        self.assertIn("Observed time in visible channels: **1h 30m**.", content)
        self.assertIn("**Alice** — 1h 0m (66.7% of observed time)", content)
        self.assertIn("**Bob** — 1h 0m (66.7% of observed time)", content)
        self.assertIn("**Alone** — 30m (33.3% of observed time)", content)
        self.assertIn("slices overlap", content)
        self.assertTrue(png.startswith(b"\x89PNG"))

        # People past the top slices are combined without a share of observed time.
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": member_id, "seconds": 600.0, "full_seconds": 6000.0}
            for member_id in range(50, 60)
        ]
        content, _ = await _company_report(self.bot, public, PERSON, "week", "full")
        self.assertIn("**Other people** — 5h 0m combined across 3 people", content)

    async def test_company_trend_can_count_full_shared_time(self):
        guild = SimpleNamespace(
            default_role=object(),
            get_member=lambda user_id: SimpleNamespace(display_name={41: "Alice", 42: "Bob"}.get(user_id)),
        )
        self.bot.get_guild = lambda guild_id: guild
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(view_channel=True)
        )
        # Alice was in a big group call; Bob had a shorter one-on-one.
        self.bot.store.company_rows_daily = [
            {"day": "2025-03-03", "channel_id": 101, "member_id": 41, "seconds": 600.0, "full_seconds": 3600.0},
            {"day": "2025-03-03", "channel_id": 101, "member_id": 42, "seconds": 1200.0, "full_seconds": 1200.0},
        ]
        with patch.object(commands_module.time, "time", return_value=1_741_132_800.0):
            content, _ = await _trend_report(self.bot, FakeInteraction(), PERSON, "week", "company")
            self.assertIn("Mon 3: **Bob** — 20m", content)
            content, png = await _trend_report(
                self.bot, FakeInteraction(), PERSON, "week", "company", "full"
            )
        self.assertIn("(full time with each person)", content)
        self.assertIn("Mon 3: **Alice** — 1h 0m", content)
        self.assertIn("can add up to more than the observed time", content)
        self.assertIsNotNone(png)

    async def test_leaderboard_ranks_full_shared_time_in_visible_channels(self):
        self.config.public_report_channel_ids = frozenset({20})
        names = {41: "Alice", 42: "Bob", 43: "Cara", 44: "Hidden"}
        self.bot.get_guild = lambda guild_id: SimpleNamespace(
            get_member=lambda member_id: SimpleNamespace(display_name=names.get(member_id, f"P{member_id}"))
        )
        role = SimpleNamespace(id=0)
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(view_channel=channel_id == 40)
        )
        # Alice and Cara shared a one-hour group call; Bob had 50 minutes one on one.
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": 0, "seconds": 36000.0, "full_seconds": 36000.0},
            {"channel_id": 40, "member_id": 41, "seconds": 1800.0, "full_seconds": 3600.0},
            {"channel_id": 40, "member_id": 42, "seconds": 1800.0, "full_seconds": 1800.0},
            {"channel_id": 40, "member_id": 42, "seconds": 1200.0, "full_seconds": 1200.0},
            {"channel_id": 40, "member_id": 43, "seconds": 1800.0, "full_seconds": 3600.0},
            # A row with no whole shared time is skipped.
            {"channel_id": 40, "member_id": 45, "seconds": 9000.0, "full_seconds": 0.0},
            {"channel_id": 41, "member_id": 44, "seconds": 99999.0, "full_seconds": 99999.0},
        ]
        public = FakeInteraction(channel_id=20)
        public.guild = SimpleNamespace(default_role=role)

        content = await _leaderboard_text(self.bot, public, PERSON, "all")

        lines = content.splitlines()
        self.assertEqual(lines[0], "**Leland's voice leaderboard — all time**")
        self.assertEqual(lines[1], "🥇 **Alice** — 1h 0m")
        self.assertEqual(lines[2], "🥈 **Cara** — 1h 0m")
        self.assertEqual(lines[3], "🥉 **Bob** — 50m")
        self.assertEqual(lines[4], "Time alone (not ranked): 10h 0m.")
        self.assertNotIn("Hidden", content)
        self.assertNotIn("P45", content)
        self.assertTrue(self.bot.store.include_live)

    async def test_leaderboard_counts_people_beyond_the_top_ten(self):
        self.bot.get_guild = lambda guild_id: SimpleNamespace(
            get_member=lambda member_id: SimpleNamespace(display_name=f"Friend {member_id}")
        )
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(view_channel=True)
        )
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": member_id, "seconds": 30.0 * member_id,
             "full_seconds": 60.0 * member_id}
            for member_id in range(1, 13)
        ]
        interaction = FakeInteraction()
        interaction.guild = SimpleNamespace(default_role=SimpleNamespace(id=0))

        content = await _leaderboard_text(self.bot, interaction, PERSON, "week")

        self.assertIn("**Leland's voice leaderboard — this week**", content)
        self.assertIn("🥇 **Friend 12** — 12m", content)
        self.assertIn("10. **Friend 3** — 3m", content)
        self.assertNotIn("Friend 2**", content)
        self.assertIn("…and 2 others.", content)
        self.assertNotIn("Time alone", content)

    async def test_leaderboard_without_companions(self):
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(view_channel=True)
        )
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": 0, "seconds": 600.0, "full_seconds": 600.0}
        ]
        interaction = FakeInteraction()
        interaction.guild = SimpleNamespace(default_role=SimpleNamespace(id=0))
        content = await _leaderboard_text(self.bot, interaction, PERSON, "today")
        self.assertIn("No time with other people has been observed for Leland today in voice", content)

    async def test_company_chart_fetches_uncached_names_after_voice_departure(self):
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": member_id, "seconds": 10}
            for member_id in (41, 42, 43)
        ] + [{"channel_id": 41, "member_id": 44, "seconds": 10}]
        role = SimpleNamespace(id=0)
        guild = SimpleNamespace(get_member=lambda member_id: None)

        async def fetch_member(member_id):
            if member_id == 41:
                return SimpleNamespace(display_name="Server nickname")
            raise TimeoutError

        async def fetch_user(member_id):
            if member_id == 42:
                return SimpleNamespace(display_name="Global name")
            raise TimeoutError

        guild.fetch_member = AsyncMock(side_effect=fetch_member)
        self.bot.get_guild = lambda guild_id: guild
        self.bot.get_user = lambda member_id: None
        self.bot.fetch_user = AsyncMock(side_effect=fetch_user)
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(view_channel=channel_id == 40)
        )
        public = FakeInteraction(channel_id=20)
        public.guild = SimpleNamespace(default_role=role)

        content, png = await _company_report(self.bot, public, PERSON, "week")

        self.assertIn("Server nickname", content)
        self.assertIn("Global name", content)
        self.assertIn("User 43", content)
        self.assertNotIn("User 44", content)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(
            [call.args[0] for call in guild.fetch_member.await_args_list],
            [41, 42, 43],
        )
        self.assertEqual(
            [call.args[0] for call in self.bot.fetch_user.await_args_list],
            [42, 43],
        )

    def test_report_visibility_supports_all_channels_and_legacy_output_channel(self):
        self.config.public_report_channel_ids = None
        self.assertFalse(_report_is_ephemeral(self.bot, FakeInteraction(channel_id=21)))
        self.config.output_channel_id = 20
        self.config.public_report_channel_ids = frozenset()
        self.assertFalse(_report_is_ephemeral(self.bot, FakeInteraction(channel_id=20)))

    async def test_only_configured_users_can_control(self):
        target = FakeInteraction(user=SimpleNamespace(id=30, guild_permissions=SimpleNamespace(manage_guild=False)))
        owner = FakeInteraction(user=SimpleNamespace(id=31, guild_permissions=SimpleNamespace(manage_guild=False)))
        admin = FakeInteraction(user=SimpleNamespace(id=33, guild_permissions=SimpleNamespace(manage_guild=False)))
        manager = FakeInteraction(user=SimpleNamespace(id=34, guild_permissions=SimpleNamespace(manage_guild=True)))
        member = FakeInteraction(user=SimpleNamespace(id=32, guild_permissions=SimpleNamespace(manage_guild=False)))
        self.assertFalse(await _can_control(target, self.bot))
        self.assertTrue(await _can_control(owner, self.bot))
        self.assertTrue(await _can_control(admin, self.bot))
        self.assertFalse(await _can_control(manager, self.bot))
        self.assertFalse(await _can_control(member, self.bot))

    async def test_only_owner_can_grant_and_revoke_admins(self):
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        admin = next(command for command in flock.commands if command.name == "admin")
        commands = {command.name: command for command in admin.commands}
        owner = FakeInteraction(user=SimpleNamespace(id=31))
        member = SimpleNamespace(id=35, bot=False)
        await commands["add"].callback(owner, member)
        self.assertTrue(owner.response.deferred)
        self.assertTrue(owner.followup.sent[0]["ephemeral"])
        self.assertTrue(await _can_control(FakeInteraction(user=member), self.bot))

        listed = FakeInteraction(user=SimpleNamespace(id=31))
        await commands["list"].callback(listed)
        self.assertIn("Owner: Unknown user — 31", listed.followup.sent[0]["content"])
        self.assertIn("\nUnknown user — 35", listed.followup.sent[0]["content"])
        self.assertTrue(listed.followup.sent[0]["ephemeral"])

        other_admin = FakeInteraction(user=SimpleNamespace(id=33))
        await commands["add"].callback(other_admin, SimpleNamespace(id=36, bot=False))
        self.assertIn("Only the configured tracker owner", other_admin.response.sent[0]["content"])
        self.assertNotIn(36, self.bot.store.admin_decisions)
        denied_list = FakeInteraction(user=SimpleNamespace(id=33))
        await commands["list"].callback(denied_list)
        self.assertIn("Only the configured tracker owner", denied_list.response.sent[0]["content"])

        revoked = FakeInteraction(user=SimpleNamespace(id=31))
        await commands["remove"].callback(revoked, "<@35>")
        self.assertFalse(await _can_control(FakeInteraction(user=member), self.bot))
        self.assertTrue(revoked.followup.sent[0]["ephemeral"])

    async def test_version_command_replies_privately_with_version(self):
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        version = next(command for command in flock.commands if command.name == "version")
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await version.callback(interaction)
        sent = interaction.followup.sent[0]
        self.assertIn(flock_cctv.version_string(), sent["content"])
        self.assertTrue(sent["ephemeral"])

    async def test_about_command_replies_privately_with_status(self):
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        about = next(command for command in flock.commands if command.name == "about")
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await about.callback(interaction)
        sent = interaction.followup.sent[0]
        self.assertIn("**About Flock**", sent["content"])
        self.assertIn(f"Version: **{flock_cctv.version_string()}**", sent["content"])
        self.assertIn("Collection: **running**", sent["content"])
        self.assertTrue(sent["ephemeral"])

    async def test_admin_add_and_remove_replies_name_the_user(self):
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        admin = next(command for command in flock.commands if command.name == "admin")
        commands = {command.name: command for command in admin.commands}
        guild = SimpleNamespace(
            get_member=lambda user_id: None,
            fetch_member=AsyncMock(side_effect=TimeoutError),
        )
        self.bot.get_guild = lambda guild_id: guild
        self.bot.get_user = lambda user_id: None
        self.bot.fetch_user = AsyncMock(
            return_value=SimpleNamespace(display_name="Former", name="left_server")
        )

        added = FakeInteraction(user=SimpleNamespace(id=31))
        member = SimpleNamespace(id=35, bot=False, display_name="Nick", name="joined")
        await commands["add"].callback(added, member)
        self.assertEqual(
            added.followup.sent[0]["content"],
            "Nick (@joined) — 35 now has tracker admin access.",
        )
        self.bot.fetch_user.assert_not_awaited()

        removed = FakeInteraction(user=SimpleNamespace(id=31))
        await commands["remove"].callback(removed, "35")
        self.assertEqual(
            removed.followup.sent[0]["content"],
            "Former (@left\\_server) — 35 no longer has tracker admin access.",
        )
        self.assertFalse(self.bot.store.admin_decisions[35])

        self.bot.fetch_user.side_effect = TimeoutError
        unknown = FakeInteraction(user=SimpleNamespace(id=31))
        await commands["remove"].callback(unknown, "36")
        self.assertEqual(
            unknown.followup.sent[0]["content"],
            "Unknown user — 36 no longer has tracker admin access.",
        )

    async def test_admin_list_shows_display_names_and_usernames(self):
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        admin = next(command for command in flock.commands if command.name == "admin")
        list_command = next(command for command in admin.commands if command.name == "list")
        self.bot.store.admin_decisions = {34: True, 35: True, 36: True}
        cached = {
            31: SimpleNamespace(display_name="Owner", name="owner"),
            33: SimpleNamespace(display_name="Nick *bold*", name="env_admin"),
        }
        guild = SimpleNamespace(get_member=cached.get)

        async def fetch_member(user_id):
            raise TimeoutError

        async def fetch_user(user_id):
            if user_id == 34:
                return SimpleNamespace(display_name="Global", name="departed")
            if user_id == 35:
                return SimpleNamespace(display_name="plain", name="plain")
            raise TimeoutError

        guild.fetch_member = AsyncMock(side_effect=fetch_member)
        self.bot.get_guild = lambda guild_id: guild
        self.bot.get_user = lambda user_id: None
        self.bot.fetch_user = AsyncMock(side_effect=fetch_user)

        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await list_command.callback(interaction)

        content = interaction.followup.sent[0]["content"]
        self.assertEqual(
            content.splitlines(),
            [
                "Owner: Owner (@owner) — 31",
                "Tracker admins:",
                "Nick \\*bold\\* (@env\\_admin) — 33",
                "Global (@departed) — 34",
                "@plain — 35",
                "Unknown user — 36",
            ],
        )
        self.assertTrue(interaction.followup.sent[0]["ephemeral"])
        self.assertEqual(
            sorted(call.args[0] for call in self.bot.fetch_user.await_args_list),
            [34, 35, 36],
        )

    async def test_admin_revocation_overrides_environment_and_delete_confirmation(self):
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        admin = next(command for command in flock.commands if command.name == "admin")
        remove = next(command for command in admin.commands if command.name == "remove")
        await remove.callback(FakeInteraction(user=SimpleNamespace(id=31)), "33")
        self.assertFalse(await _can_control(FakeInteraction(user=SimpleNamespace(id=33)), self.bot))
        view = DeleteDataConfirmation(self.bot, invoker_id=33)
        confirmation = FakeInteraction(user=SimpleNamespace(id=33))
        await view.children[0].callback(confirmation)
        self.assertIn("Only configured tracker admins", confirmation.response.sent[0]["content"])

    async def test_admin_commands_reject_invalid_targets_and_wrong_scope(self):
        register_commands(self.bot)
        flock = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        admin = next(command for command in flock.commands if command.name == "admin")
        commands = {command.name: command for command in admin.commands}
        owner = SimpleNamespace(id=31)
        for user in (SimpleNamespace(id=30, bot=False), SimpleNamespace(id=31, bot=False), SimpleNamespace(id=35, bot=True)):
            denied = FakeInteraction(user=owner)
            await commands["add"].callback(denied, user)
            self.assertIn("Choose a human member", denied.response.sent[0]["content"])
        denied = FakeInteraction(user=owner)
        await commands["remove"].callback(denied, "31")
        self.assertIn("owner cannot be removed", denied.response.sent[0]["content"])
        denied = FakeInteraction(user=owner)
        await commands["remove"].callback(denied, "invalid")
        self.assertIn("positive Discord user ID", denied.response.sent[0]["content"])
        denied = FakeInteraction(guild_id=999, user=owner)
        await commands["add"].callback(denied, SimpleNamespace(id=35, bot=False))
        self.assertIn("configured server", denied.response.sent[0]["content"])
        self.assertEqual(self.bot.store.admin_decisions, {})

    async def test_control_commands_reject_server_manager_and_target(self):
        register_commands(self.bot)
        group = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        self.bot.tracker.pause = AsyncMock()
        self.bot.tracker.resume = AsyncMock()
        self.bot.tracker.delete_data = AsyncMock()
        self.bot.tracker.delete_user_data = AsyncMock()
        for user_id, manage_guild in ((30, False), (34, True)):
            for name in ("pause", "resume", "delete-data", "update"):
                with self.subTest(user_id=user_id, command=name):
                    interaction = FakeInteraction(user=SimpleNamespace(
                        id=user_id,
                        guild_permissions=SimpleNamespace(manage_guild=manage_guild),
                    ))
                    command = next(command for command in group.commands if command.name == name)
                    await command.callback(interaction)
                    self.assertIn("Only configured tracker admins", interaction.response.sent[0]["content"])
            with self.subTest(user_id=user_id, command="delete-data user"):
                interaction = FakeInteraction(user=SimpleNamespace(id=user_id))
                command = next(command for command in group.commands if command.name == "delete-data")
                await command.callback(interaction, user=_user(41, "Ana"))
                self.assertIn("Only configured tracker admins", interaction.response.sent[0]["content"])
                self.assertNotIn("view", interaction.response.sent[0])
        self.bot.tracker.pause.assert_not_awaited()
        self.bot.tracker.resume.assert_not_awaited()
        self.bot.tracker.delete_data.assert_not_awaited()
        self.bot.tracker.delete_user_data.assert_not_awaited()

    async def test_owner_and_extra_admin_can_pause_and_resume(self):
        register_commands(self.bot)
        group = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        self.bot.tracker.pause = AsyncMock()
        self.bot.tracker.resume = AsyncMock()
        snapshot = {41: 7, 99: 7}
        self.bot.voice_snapshot = lambda: snapshot
        self.bot.tracker.tracked_ids = frozenset({41})
        replies = {}
        for name, user_id in (("pause", 31), ("resume", 33)):
            interaction = FakeInteraction(user=SimpleNamespace(id=user_id))
            command = next(command for command in group.commands if command.name == name)
            await command.callback(interaction)
            self.assertTrue(interaction.response.deferred)
            self.assertTrue(interaction.followup.sent[0]["ephemeral"])
            replies[name] = interaction.followup.sent[0]["content"]
        self.bot.tracker.pause.assert_awaited_once_with(actor_id=31)
        self.bot.tracker.resume.assert_awaited_once_with(33, self.bot.voice_snapshot)
        # Only tracked people in voice are mentioned; the untracked member 99 is not counted.
        self.assertIn("1 tracked person currently in voice is being observed", replies["resume"])

    async def test_resume_reply_when_nobody_tracked_is_in_voice(self):
        self.bot.tracker.resume = AsyncMock()
        self.bot.voice_snapshot = lambda: {99: 7}
        self.bot.tracker.tracked_ids = frozenset({41})
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await self.command("resume").callback(interaction)
        self.assertIn("will start when a tracked person joins", interaction.followup.sent[0]["content"])
        self.bot.tracker.resume.assert_awaited_once_with(31, self.bot.voice_snapshot)

    async def test_update_command_requests_a_check_privately_unless_held(self):
        import json
        import tempfile
        from pathlib import Path

        register_commands(self.bot)
        group = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        command = next(command for command in group.commands if command.name == "update")
        with tempfile.TemporaryDirectory() as name:
            self.config.database_path = Path(name) / "tracker.sqlite3"
            request = self.config.database_path.with_name("update-requested.json")
            denied = FakeInteraction(user=SimpleNamespace(id=30))
            await command.callback(denied)
            self.assertIn("Only configured tracker admins", denied.response.sent[0]["content"])
            self.assertFalse(request.exists())

            interaction = FakeInteraction(user=SimpleNamespace(id=33))
            await command.callback(interaction)
            sent = interaction.followup.sent[0]
            self.assertIn("Update check requested", sent["content"])
            self.assertTrue(sent["ephemeral"])
            self.assertIn("requested_at", json.loads(request.read_text(encoding="utf-8")))

            request.unlink()
            self.config.database_path.with_name("update-status.json").write_text(
                json.dumps({"result": "held", "revision": "a" * 40}), encoding="utf-8"
            )
            held = FakeInteraction(user=SimpleNamespace(id=31))
            await command.callback(held)
            self.assertIn("held", held.followup.sent[0]["content"])
            self.assertFalse(request.exists())

    async def test_evil_mode_toggle_is_admin_only(self):
        register_commands(self.bot)
        group = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        command = next(command for command in group.commands if command.name == "evil-mode")
        self.bot.store.set_evil_mode = AsyncMock()
        denied = FakeInteraction(user=SimpleNamespace(id=30))
        await command.callback(denied, "on")
        self.assertIn("Only configured tracker admins", denied.response.sent[0]["content"])
        self.bot.store.set_evil_mode.assert_not_awaited()
        allowed = FakeInteraction(user=SimpleNamespace(id=31))
        await command.callback(allowed, "on")
        self.bot.store.set_evil_mode.assert_awaited_once_with(True)
        self.assertIn("**on**", allowed.followup.sent[0]["content"])

    async def test_reaction_mode_toggle_is_admin_only_and_private(self):
        register_commands(self.bot)
        group = next(group for group, _ in self.bot.tree.commands if group.name == "flock")
        command = next(command for command in group.commands if command.name == "reaction-mode")
        self.bot.store.set_reaction_mode = AsyncMock()
        denied = FakeInteraction(user=SimpleNamespace(id=30))
        await command.callback(denied, "on")
        self.assertIn("Only configured tracker admins", denied.response.sent[0]["content"])
        self.bot.store.set_reaction_mode.assert_not_awaited()
        allowed = FakeInteraction(user=SimpleNamespace(id=31))
        await command.callback(allowed, "on")
        self.bot.store.set_reaction_mode.assert_awaited_once_with(True)
        self.assertTrue(allowed.followup.sent[0]["ephemeral"])
        self.assertIn("**on**", allowed.followup.sent[0]["content"])

    async def test_stats_are_aggregated_and_show_gaps(self):
        report = await _stats_text(self.bot, PERSON, "week")
        self.assertIn("4**", report)
        self.assertIn("Observed voice time", report)
        self.assertIn("Missing coverage during this period", report)
        self.assertNotIn("private-channel-marker", report)

    async def test_status_reports_auto_update_failures_without_mentions(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as name:
            self.config.database_path = Path(name) / "tracker.sqlite3"
            self.assertIn("Auto-update: **no status recorded**", await _status_text(self.bot))
            self.config.database_path.with_name("update-status.json").write_text(json.dumps({
                "result": "failed", "failures": 2, "failing_since": 1_700_000_000.0,
                "checked_at": 1_700_003_600.0, "message": "boom @everyone",
            }), encoding="utf-8")
            report = await _status_text(self.bot)
        self.assertIn("Auto-update: **failing** since", report)
        self.assertIn("(2 attempts;", report)
        self.assertNotIn("@everyone", report)

    async def test_status_reports_collection_unavailable_during_outage(self):
        self.bot.tracker.connected = False
        report = await _status_text(self.bot)
        self.assertIn(f"Version: **{flock_cctv.version_string()}**", report)
        self.assertIn("Collection: **unavailable**", report)
        self.assertIn("Message collector: **unavailable**", report)
        self.assertIn("Tracked people: **2**", report)

    async def test_seen_reports_last_channel_and_date_to_allowed_viewer(self):
        self.bot.store.last_voice_result = {
            "channel_id": 40, "seen_at": 1_700_000_000,
            "current": False, "observed_since": None,
        }
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            name="general-voice",
            permissions_for=lambda user: SimpleNamespace(view_channel=True),
        )
        with patch("flock_cctv.commands.time.time", return_value=1_700_003_600):
            report = await _seen_text(self.bot, FakeInteraction(), PERSON)
        self.assertIn("#general-voice", report)
        self.assertIn("1h", report)
        self.assertIn("Nov 14, 2023", report)

    async def test_public_where_shows_channel_name_only_when_everyone_can_view_it(self):
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.last_voice_result = {
            "channel_id": 40, "seen_at": 1_700_000_000,
            "current": False, "observed_since": None,
        }
        default_role = object()
        interaction = FakeInteraction(channel_id=20)
        interaction.guild = SimpleNamespace(default_role=default_role)
        everyone_can_view = False
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            name="private-voice",
            permissions_for=lambda viewer: SimpleNamespace(
                view_channel=viewer is interaction.user or everyone_can_view
            ),
        )
        with patch("flock_cctv.commands.time.time", return_value=1_700_003_600):
            hidden = await _seen_text(self.bot, interaction, PERSON)
            everyone_can_view = True
            visible = await _seen_text(self.bot, interaction, PERSON)
        self.assertIn("a voice channel", hidden)
        self.assertNotIn("private-voice", hidden)
        self.assertIn("#private-voice", visible)

    async def test_seen_hides_channel_name_and_disables_live_on_outage(self):
        self.bot.store.last_voice_result = {
            "channel_id": 40, "seen_at": 1_700_000_000,
            "current": False, "observed_since": None,
        }
        self.bot.tracker.collection_ready = False
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            name="private-voice",
            permissions_for=lambda user: SimpleNamespace(view_channel=False),
        )
        with patch("flock_cctv.commands.time.time", return_value=1_700_003_600):
            report = await _seen_text(self.bot, FakeInteraction(), PERSON)
        self.assertIn("a voice channel", report)
        self.assertNotIn("private-voice", report)
        self.assertFalse(self.bot.store.include_live)

    async def test_seen_handles_current_and_empty_observation(self):
        self.assertIn("haven't observed", await _seen_text(self.bot, FakeInteraction(), PERSON))
        self.bot.store.last_voice_result = {
            "channel_id": 40, "seen_at": 1_700_003_600,
            "current": True, "observed_since": 1_700_000_000,
        }
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            name="voice", permissions_for=lambda user: SimpleNamespace(view_channel=True),
        )
        self.assertIn("currently in **#voice**", await _seen_text(self.bot, FakeInteraction(), PERSON))

    async def test_online_counts_away_and_dnd_as_online(self):
        guild = SimpleNamespace(query_members=AsyncMock())
        self.bot.get_guild = lambda guild_id: guild
        for status in (discord.Status.online, discord.Status.idle, discord.Status.dnd):
            with self.subTest(status=status):
                guild.query_members.return_value = [SimpleNamespace(id=30, status=status)]
                self.assertIn("online", await _online_text(self.bot, PERSON))
        guild.query_members.assert_awaited_with(user_ids=[30], presences=True, cache=False)

    async def test_online_reports_offline_and_unknown_without_guessing(self):
        guild = SimpleNamespace(query_members=AsyncMock())
        self.bot.get_guild = lambda guild_id: guild
        guild.query_members.return_value = [SimpleNamespace(id=30, status=discord.Status.offline)]
        self.assertIn("offline or invisible", await _online_text(self.bot, PERSON))
        guild.query_members.return_value = []
        self.assertIn("couldn't find", await _online_text(self.bot, PERSON))
        guild.query_members.side_effect = TimeoutError()
        self.assertIn("couldn't check", await _online_text(self.bot, PERSON))
        self.bot.get_guild = lambda guild_id: None
        self.assertIn("server is unavailable", await _online_text(self.bot, PERSON))

    async def test_failed_recovery_disables_live_totals_and_is_visible_in_reports(self):
        self.bot.tracker.collection_ready = False
        report = await _stats_text(self.bot, PERSON, "week")
        self.assertFalse(self.bot.store.include_live)
        self.assertIn("Collection is recovering", report)
        self.assertIn("Collection: **unavailable**", await _status_text(self.bot))

    async def test_delete_confirmation_rejects_another_user(self):
        view = DeleteDataConfirmation(self.bot, invoker_id=30)
        interaction = FakeInteraction(user=SimpleNamespace(id=31, guild_permissions=SimpleNamespace(manage_guild=True)))
        button = view.children[0]
        await button.callback(interaction)
        self.assertIn("Only the person", interaction.response.sent[0]["content"])
        self.assertEqual(self.bot.store.deleted, [])

    async def test_delete_confirmation_rechecks_admin_allowlist(self):
        view = DeleteDataConfirmation(self.bot, invoker_id=33)
        self.config.admin_user_ids = frozenset()
        interaction = FakeInteraction(user=SimpleNamespace(id=33, guild_permissions=SimpleNamespace(manage_guild=True)))
        await view.children[0].callback(interaction)
        self.assertIn("Only configured tracker admins", interaction.response.sent[0]["content"])
        self.assertEqual(self.bot.store.deleted, [])

    async def test_resume_permission_error_gets_clear_response(self):
        interaction = FakeInteraction()

        async def action():
            raise PermissionError("opaque detail")

        await _execute_after_scope(interaction, self.bot, "flock resume", action, ephemeral=True)
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn("You do not have permission", interaction.followup.sent[0]["content"])
        self.assertNotIn("opaque detail", interaction.followup.sent[0]["content"])

    async def test_duplicate_delete_confirmation_is_one_shot(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class SlowTracker:
            calls = 0

            async def delete_data(inner_self, actor_id):
                inner_self.calls += 1
                started.set()
                await release.wait()

        self.bot.tracker = SlowTracker()
        view = DeleteDataConfirmation(self.bot, invoker_id=31)
        user = SimpleNamespace(id=31, guild_permissions=SimpleNamespace(manage_guild=False))
        first = FakeInteraction(user=user)
        second = FakeInteraction(user=user)
        first_task = asyncio.create_task(view.children[0].callback(first))
        await started.wait()
        await view.children[0].callback(second)
        self.assertIn("already being handled", second.response.sent[0]["content"])
        self.assertEqual(self.bot.tracker.calls, 1)
        release.set()
        await first_task
        self.assertEqual(self.bot.tracker.calls, 1)
        self.assertEqual(first.edited[0]["view"], None)

    # -- Per-person reports: the ``user`` option -------------------------------

    _PERSON_COMMANDS = ("stats", "records", "where", "company", "leaderboard", "trends", "roast")

    def _no_cooldown(self):
        return patch.object(
            commands_module, "_ROAST_COOLDOWN", SimpleNamespace(consume=AsyncMock(return_value=0))
        )

    async def test_user_option_defaults_to_requester_and_reports_use_that_user_id(self):
        self.config.public_report_channel_ids = frozenset({20})
        flock = self.group()
        with self._no_cooldown():
            for name in self._PERSON_COMMANDS:
                command = next(command for command in flock.commands if command.name == name)
                for target, expected in ((None, 30), (_user(41, "Ana"), 41)):
                    with self.subTest(command=name, user=expected):
                        self.bot.store.user_ids.clear()
                        interaction = FakeInteraction(channel_id=20, user=_user(30, "Leland"))
                        if target is None:
                            await command.callback(interaction)
                        else:
                            await command.callback(interaction, user=target)
                        self.assertTrue(interaction.response.deferred)
                        self.assertEqual(len(interaction.followup.sent), 1)
                        self.assertNotIn("isn't tracked", interaction.followup.sent[0]["content"])
                        self.assertTrue(self.bot.store.user_ids)
                        self.assertEqual(set(self.bot.store.user_ids), {expected})

    async def test_stats_title_uses_the_chosen_persons_name(self):
        stats = self.command("stats")
        own = FakeInteraction(user=_user(30, "Leland"))
        await stats.callback(own)
        self.assertTrue(own.followup.sent[0]["content"].startswith("**The Leland Report — this week**"))
        other = FakeInteraction(user=_user(30, "Leland"))
        await stats.callback(other, period="month", user=_user(41, "Ana"))
        content = other.followup.sent[0]["content"]
        self.assertTrue(content.startswith("**The Ana Report — this month**"))
        self.assertNotIn("Leland", content)
        self.assertNotIn("no longer tracked", content)

    async def test_requester_without_a_display_name_falls_back_to_the_user_id(self):
        interaction = FakeInteraction()  # default requester has only an ID
        await self.command("stats").callback(interaction)
        self.assertTrue(interaction.followup.sent[0]["content"].startswith("**The User 30 Report"))

    async def test_bots_and_untracked_people_get_private_replies_before_any_report(self):
        self.config.public_report_channel_ids = frozenset({20})
        flock = self.group()
        cases = [
            (_user(900, "Robo", bot=True), "Bots aren't tracked."),
            (
                _user(77, "Zed *x*"),
                "Zed \\*x\\* isn't tracked. An admin can add them with `/flock track add`.",
            ),
        ]
        guild = _fake_guild(self.bot)
        guild.query_members = AsyncMock()
        cooldown = SimpleNamespace(consume=AsyncMock(return_value=0))
        with patch.object(commands_module, "_ROAST_COOLDOWN", cooldown):
            for name in (*self._PERSON_COMMANDS, "online"):
                command = next(command for command in flock.commands if command.name == name)
                for target, expected in cases:
                    with self.subTest(command=name, user=target.id):
                        interaction = FakeInteraction(channel_id=20)
                        await command.callback(interaction, user=target)
                        sent = interaction.response.sent[0]
                        self.assertEqual(sent["content"], expected)
                        self.assertTrue(sent["ephemeral"])
                        self.assertFalse(interaction.response.deferred)
                        self.assertEqual(interaction.followup.sent, [])
        self.assertEqual(self.bot.store.user_ids, [])
        cooldown.consume.assert_not_awaited()
        guild.query_members.assert_not_awaited()

    async def test_formerly_tracked_people_get_a_report_with_a_note(self):
        self.config.public_report_channel_ids = frozenset({20})
        flock = self.group()
        note = "Bob is no longer tracked; showing recorded history."
        with self._no_cooldown():
            for name in self._PERSON_COMMANDS:
                command = next(command for command in flock.commands if command.name == name)
                with self.subTest(command=name):
                    self.bot.store.user_ids.clear()
                    interaction = FakeInteraction(channel_id=20)
                    await command.callback(interaction, user=_user(42, "Bob"))
                    sent = interaction.followup.sent[0]
                    self.assertTrue(sent["content"].endswith("\n" + note))
                    self.assertFalse(sent["ephemeral"])
                    self.assertEqual(set(self.bot.store.user_ids), {42})

    async def test_online_checks_only_active_tracked_people(self):
        self.config.public_report_channel_ids = frozenset({20})
        guild = _fake_guild(self.bot)

        async def query(user_ids, presences, cache):
            return [SimpleNamespace(id=user_ids[0], status=discord.Status.idle)]

        guild.query_members = AsyncMock(side_effect=query)
        online = self.command("online")
        own = FakeInteraction(channel_id=20, user=_user(30, "Leland"))
        await online.callback(own)
        self.assertEqual(guild.query_members.await_args.kwargs["user_ids"], [30])
        self.assertEqual(own.followup.sent[0]["content"], "Leland is online (away) right now.")
        other = FakeInteraction(channel_id=20, user=_user(30, "Leland"))
        await online.callback(other, user=_user(41, "Ana"))
        self.assertEqual(guild.query_members.await_args.kwargs["user_ids"], [41])
        self.assertEqual(other.followup.sent[0]["content"], "Ana is online (away) right now.")
        # Presence is private even where reports are public.
        self.assertTrue(own.followup.sent[0]["ephemeral"])
        self.assertTrue(other.followup.sent[0]["ephemeral"])

        checked = guild.query_members.await_count
        for target, expected in (
            (_user(77, "Zed"), "Zed isn't tracked. An admin can add them with `/flock track add`."),
            (
                _user(42, "Bob"),
                "Bob is no longer tracked. An admin can add them again with `/flock track add`.",
            ),
            (_user(900, "Robo", bot=True), "Bots aren't tracked."),
        ):
            refused = FakeInteraction(channel_id=20)
            await online.callback(refused, user=target)
            self.assertEqual(refused.response.sent[0]["content"], expected)
            self.assertTrue(refused.response.sent[0]["ephemeral"])
        self.assertEqual(guild.query_members.await_count, checked)

    async def test_names_are_escaped_and_never_mention(self):
        tricky = "**Ana** @everyone <@123456789012345678>\n_x_"
        self.bot.store.tracked.append(_row(50))
        interaction = FakeInteraction()
        await self.command("stats").callback(interaction, user=_user(50, tricky))
        sent = interaction.followup.sent[0]
        title = sent["content"].splitlines()[0]
        self.assertTrue(title.startswith("**The \\*\\*Ana\\*\\* "), title)
        self.assertIn("\\_x\\_ Report — this week**", title)
        self.assertNotIn("@everyone", sent["content"])
        self.assertIsNone(re.search(r"<@[!&]?\d+>", sent["content"]))
        mentions = sent["allowed_mentions"]
        self.assertFalse(mentions.everyone or mentions.users or mentions.roles)

        refused = FakeInteraction()
        await self.command("stats").callback(refused, user=_user(51, tricky))
        self.assertNotIn("@everyone", refused.response.sent[0]["content"])
        self.assertIsNone(re.search(r"<@[!&]?\d+>", refused.response.sent[0]["content"]))

        person = commands_module._Person(50, commands_module._clean_name(tricky), True, 0.0)
        self.bot.store.trend_rows = [
            {"day": "2025-03-03", "messages": 4, "voice_seconds": 60.0, "voice_visits": 1},
        ]
        content, png = await _trend_report(self.bot, FakeInteraction(), person, "week", "daily")
        self.assertNotIn("@everyone", content)
        self.assertIsNone(re.search(r"<@[!&]?\d+>", content))
        self.assertTrue(content.startswith("**\\*\\*Ana\\*\\* "))
        self.assertTrue(png.startswith(b"\x89PNG"))

    async def test_names_are_collapsed_to_one_short_line(self):
        self.bot.store.tracked.append(_row(50))
        interaction = FakeInteraction()
        await self.command("stats").callback(interaction, user=_user(50, "Line one\n@here\r\n" + "x" * 80))
        title = interaction.followup.sent[0]["content"].splitlines()[0]
        cleaned = commands_module._clean_name("Line one\n@here\r\n" + "x" * 80)
        self.assertEqual(len(cleaned), 48)
        self.assertNotIn("\n", cleaned)
        self.assertEqual(title, f"**The {commands_module._safe_name(cleaned)} Report — this week**")

    async def test_pie_title_with_a_long_name_stays_inside_the_canvas(self):
        png = _pie_png([("Alice", 90), ("Alone", 30)], "W" * 48)
        with Image.open(BytesIO(png)) as chart:
            title_band = chart.convert("L").crop((0, 0, chart.width, 90))
            right_edge = title_band.crop((chart.width - 40, 0, chart.width, 90))
            self.assertEqual(min(right_edge.getdata()), 255)
            self.assertLess(min(title_band.crop((44, 20, 400, 80)).getdata()), 100)

    async def test_wrong_scope_stops_person_commands_before_any_lookup(self):
        flock = self.group()
        for name in (*self._PERSON_COMMANDS, "online", "top"):
            command = next(command for command in flock.commands if command.name == name)
            wrong_guild = FakeInteraction(guild_id=99)
            await command.callback(wrong_guild)
            self.assertIn("configured server", wrong_guild.response.sent[0]["content"])
            self.assertTrue(wrong_guild.response.sent[0]["ephemeral"])
        self.config.output_channel_id = 22
        wrong_channel = FakeInteraction(channel_id=20)
        await self.command("stats").callback(wrong_channel, user=_user(41, "Ana"))
        self.assertIn("configured tracker channel", wrong_channel.response.sent[0]["content"])
        self.assertEqual(self.bot.store.user_ids, [])

    async def test_failed_report_logs_the_command_and_replies_safely(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("boom secret")

        self.bot.store.stats = broken
        interaction = FakeInteraction()
        with self.assertLogs("flock_cctv.commands", level="ERROR") as logs:
            await self.command("stats").callback(interaction, user=_user(41, "Ana"))
        self.assertIn("Slash command flock stats failed", logs.output[0])
        sent = interaction.followup.sent[0]
        self.assertEqual(sent["content"], commands_module._FAILURE_TEXT)
        self.assertNotIn("boom", sent["content"])

        self.bot.store.tracked_users = broken
        failed_lookup = FakeInteraction(channel_id=20)
        self.config.public_report_channel_ids = frozenset({20})
        with self.assertLogs("flock_cctv.commands", level="ERROR") as logs:
            await self.command("where").callback(failed_lookup)
        self.assertIn("Slash command flock where failed", logs.output[0])
        self.assertEqual(failed_lookup.response.sent[0]["content"], commands_module._FAILURE_TEXT)
        self.assertTrue(failed_lookup.response.sent[0]["ephemeral"])

    async def test_records_measurement_period_starts_when_the_person_was_tracked(self):
        later = commands_module._Person(41, "Ana", True, 1_760_000_000.0)  # Oct 9, 2025
        earlier = commands_module._Person(41, "Ana", True, 1_600_000_000.0)  # before the database
        content = await _records_text(self.bot, FakeInteraction(), later)
        self.assertIn("**Ana's personal records**", content)
        self.assertIn("Measurement period: since Oct 9, 2025", content)
        content = await _records_text(self.bot, FakeInteraction(), earlier)
        self.assertIn("Measurement period: since Nov 14, 2023", content)
        self.assertEqual(set(self.bot.store.user_ids), {41})

    async def test_roast_addresses_the_person_and_shares_one_cooldown(self):
        roast = self.command("roast")
        with patch.object(commands_module, "_ROAST_COOLDOWN", SharedRoastCooldown(seconds=30)):
            refused = FakeInteraction()
            await roast.callback(refused, user=_user(77, "Zed"))
            self.assertIn("isn't tracked", refused.response.sent[0]["content"])
            # A refused request must not use up the shared cooldown.
            first = FakeInteraction(channel_id=20)
            await roast.callback(first, user=_user(41, "Ana"))
            self.assertTrue(first.followup.sent[0]["content"].startswith("Ana, "))
            second = FakeInteraction(channel_id=20)
            await roast.callback(second, user=_user(41, "Ana"))
            self.assertIn("shared roast cooldown", second.response.sent[0]["content"])
            self.assertTrue(second.response.sent[0]["ephemeral"])

    async def test_leaderboard_and_company_for_a_person_apply_channel_visibility(self):
        self.config.public_report_channel_ids = frozenset({20})
        role = SimpleNamespace(id=0)
        _fake_guild(self.bot, {51: _user(51, "Public friend"), 52: _user(52, "Hidden friend")})
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(
                view_channel=channel_id == 40 or viewer is not role
            )
        )
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": 51, "seconds": 60.0, "full_seconds": 60.0},
            {"channel_id": 41, "member_id": 52, "seconds": 600.0, "full_seconds": 600.0},
        ]
        ana = _user(41, "Ana")
        for name in ("leaderboard", "company", "records"):
            command = self.command(name)
            public = FakeInteraction(channel_id=20)
            public.guild = SimpleNamespace(default_role=role)
            await command.callback(public, user=ana)
            content = public.followup.sent[0]["content"]
            with self.subTest(command=name, audience="public"):
                self.assertIn("Ana's", content)
                self.assertIn("Public friend", content)
                self.assertNotIn("Hidden friend", content)
                self.assertFalse(public.followup.sent[0]["ephemeral"])
            private = FakeInteraction(channel_id=21)
            private.guild = SimpleNamespace(default_role=role)
            await command.callback(private, user=ana)
            private_content = private.followup.sent[0]["content"]
            with self.subTest(command=name, audience="private"):
                self.assertTrue(private.followup.sent[0]["ephemeral"])
                self.assertIn("Hidden friend", private_content)
                if name != "records":  # records show only the top companion
                    self.assertIn("Public friend", private_content)
        self.assertEqual(set(self.bot.store.user_ids), {41})

    async def test_company_for_a_person_attaches_a_named_chart(self):
        self.config.public_report_channel_ids = frozenset({20})
        _fake_guild(self.bot, {51: _user(51, "Friend")})
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(view_channel=True)
        )
        self.bot.store.company_rows = [{"channel_id": 40, "member_id": 51, "seconds": 60.0, "full_seconds": 60.0}]
        interaction = FakeInteraction(channel_id=20)
        interaction.guild = SimpleNamespace(default_role=object())
        await self.command("company").callback(interaction, user=_user(41, "Ana"))
        sent = interaction.followup.sent[0]
        self.assertTrue(sent["content"].startswith("**Ana's voice company — this week**"))
        self.assertEqual(sent["file"].filename, "flock-voice-company.png")

        full = FakeInteraction(channel_id=20)
        full.guild = interaction.guild
        await self.command("company").callback(full, count="full", user=_user(41, "Ana"))
        self.assertIn("(full time with each person)", full.followup.sent[0]["content"])

    # -- /flock top -------------------------------------------------------------

    async def test_top_ranks_ties_by_user_id_omits_zero_and_counts_the_rest(self):
        _fake_guild(self.bot, {number: _user(number, f"P{number}") for number in range(1, 15)})
        self.bot.store.ranking_rows = [
            _rank(14, messages=1), _rank(1, messages=5), _rank(2, messages=9), _rank(3, messages=5),
            _rank(4, messages=0, voice=100.0), _rank(5, messages=7, tracked=False),
        ] + [_rank(number, messages=1) for number in range(6, 14)]
        content = await commands_module._top_text(self.bot, "week", "messages")
        self.assertEqual(
            content.splitlines(),
            [
                "**Top messages — this week**",
                "🥇 **P2** — 9 messages",
                "🥈 **P5** — 7 messages (no longer tracked)",
                "🥉 **P1** — 5 messages",
                "4. **P3** — 5 messages",
                "5. **P6** — 1 message",
                "6. **P7** — 1 message",
                "7. **P8** — 1 message",
                "8. **P9** — 1 message",
                "9. **P10** — 1 message",
                "10. **P11** — 1 message",
                "…and 3 more.",
                "Only activity observed while each person was tracked is counted.",
            ],
        )
        self.assertNotIn("P4", content)

    async def test_top_metrics_and_alias_for_active_days(self):
        _fake_guild(self.bot, {number: _user(number, f"P{number}") for number in range(1, 5)})
        self.bot.store.ranking_rows = [
            _rank(1, messages=50, voice=90.0, days=1),
            _rank(2, messages=0, voice=3600.0, days=3),
            _rank(3, messages=5, voice=0.0, days=3),
            _rank(4),
        ]
        voice = await commands_module._top_text(self.bot, "all", "voice")
        self.assertEqual(voice.splitlines()[0], "**Top observed voice time — all time**")
        self.assertEqual(voice.splitlines()[1], "🥇 **P2** — 1h 0m")
        self.assertEqual(voice.splitlines()[2], "🥈 **P1** — 1m")
        self.assertEqual(len(voice.splitlines()), 4)
        for metric in ("active_days", "active days"):
            days = await commands_module._top_text(self.bot, "month", metric)
            self.assertEqual(
                days.splitlines()[:3],
                ["**Top active days — this month**", "🥇 **P2** — 3 days", "🥈 **P3** — 3 days"],
            )
            self.assertIn("🥉 **P1** — 1 day", days)
        self.bot.store.ranking_rows = [_rank(1), _rank(2)]
        self.assertEqual(
            await commands_module._top_text(self.bot, "today", "messages"),
            "No messages to rank for today.",
        )

    async def test_top_command_defaults_period_and_visibility_and_live_data(self):
        _fake_guild(self.bot, {1: _user(1, "Ana"), 2: _user(2, "Bob")})
        self.bot.store.ranking_rows = [_rank(1, messages=3), _rank(2, messages=2)]
        self.config.public_report_channel_ids = frozenset({20})
        top = self.command("top")
        public = FakeInteraction(channel_id=20)
        await top.callback(public)
        self.assertEqual(self.bot.store.ranking_calls, ["week"])
        self.assertFalse(public.followup.sent[0]["ephemeral"])
        self.assertIn("🥇 **Ana** — 3 messages", public.followup.sent[0]["content"])
        self.assertTrue(self.bot.store.include_live)
        private = FakeInteraction(channel_id=21)
        self.bot.tracker.collection_ready = False
        await top.callback(private, period="all", metric="voice")
        self.assertTrue(private.followup.sent[0]["ephemeral"])
        self.assertEqual(self.bot.store.ranking_calls, ["week", "all"])
        self.assertFalse(self.bot.store.include_live)

    async def test_top_escapes_names_and_survives_failures(self):
        _fake_guild(self.bot, {1: _user(1, "**Ana** @everyone")})
        self.bot.store.ranking_rows = [_rank(1, messages=3)]
        interaction = FakeInteraction()
        await self.command("top").callback(interaction)
        content = interaction.followup.sent[0]["content"]
        self.assertIn("\\*\\*Ana\\*\\*", content)
        self.assertNotIn("@everyone", content)

        async def broken(*args, **kwargs):
            raise RuntimeError("boom")

        self.bot.store.ranking = broken
        failed = FakeInteraction()
        with self.assertLogs("flock_cctv.commands", level="ERROR") as logs:
            await self.command("top").callback(failed)
        self.assertIn("Slash command flock top failed", logs.output[0])
        self.assertEqual(failed.followup.sent[0]["content"], commands_module._FAILURE_TEXT)

    # -- /flock track -----------------------------------------------------------

    async def test_track_add_is_for_owner_and_effective_admins_and_private(self):
        self.config.public_report_channel_ids = frozenset({20})
        snapshot = {41: 7}
        self.bot.voice_snapshot = lambda: snapshot
        self.bot.tracker.track_user = AsyncMock(return_value=True)
        add = self.command("add", "track")
        member = _user(35, "Nick", username="joined")
        for actor in (31, 33):
            interaction = FakeInteraction(channel_id=20, user=SimpleNamespace(id=actor))
            await add.callback(interaction, member)
            sent = interaction.followup.sent[0]
            self.assertTrue(sent["ephemeral"])
            self.assertEqual(
                sent["content"],
                "Nick (@joined) — 35 is now tracked. Their messages and voice time are counted from now.",
            )
        self.assertEqual(
            self.bot.tracker.track_user.await_args_list,
            [call(35, 31, self.bot.voice_snapshot), call(35, 33, self.bot.voice_snapshot)],
        )

        # Admins and the owner may be tracked themselves.
        own = FakeInteraction(user=SimpleNamespace(id=31))
        await add.callback(own, _user(31, "Owner"))
        self.bot.tracker.track_user.assert_awaited_with(31, 31, self.bot.voice_snapshot)

        self.bot.tracker.track_user.reset_mock()
        for actor, manage in ((32, False), (30, False), (34, True)):
            denied = FakeInteraction(
                channel_id=20,
                user=SimpleNamespace(id=actor, guild_permissions=SimpleNamespace(manage_guild=manage)),
            )
            await add.callback(denied, member)
            self.assertIn("Only configured tracker admins", denied.response.sent[0]["content"])
            self.assertTrue(denied.response.sent[0]["ephemeral"])
        self.bot.store.admin_decisions[33] = False  # revoked admin
        revoked = FakeInteraction(user=SimpleNamespace(id=33))
        await add.callback(revoked, member)
        self.assertIn("Only configured tracker admins", revoked.response.sent[0]["content"])
        self.bot.tracker.track_user.assert_not_awaited()

    async def test_track_add_rejects_bots_reports_duplicates_and_pause(self):
        self.bot.tracker.track_user = AsyncMock(return_value=True)
        add = self.command("add", "track")
        bot_member = _user(36, "Robo", bot=True)
        rejected = FakeInteraction(user=SimpleNamespace(id=31))
        await add.callback(rejected, bot_member)
        self.assertEqual(rejected.response.sent[0]["content"], "Bots can't be tracked.")
        self.assertTrue(rejected.response.sent[0]["ephemeral"])
        self.bot.tracker.track_user.assert_not_awaited()

        self.bot.tracker.track_user.return_value = False
        duplicate = FakeInteraction(user=SimpleNamespace(id=31))
        await add.callback(duplicate, _user(35, "Nick", username="joined"))
        self.assertEqual(duplicate.followup.sent[0]["content"], "Nick (@joined) — 35 is already tracked.")

        self.bot.tracker.track_user.return_value = True
        self.bot.store.paused = True
        paused = FakeInteraction(user=SimpleNamespace(id=31))
        await add.callback(paused, _user(35, "Nick", username="joined"))
        self.assertIn("counting starts when it resumes", paused.followup.sent[0]["content"])

        wrong = FakeInteraction(guild_id=99, user=SimpleNamespace(id=31))
        await add.callback(wrong, _user(35, "Nick"))
        self.assertIn("configured server", wrong.response.sent[0]["content"])
        self.assertEqual(self.bot.tracker.track_user.await_count, 2)

    async def test_track_add_failure_is_logged_and_private(self):
        self.bot.tracker.track_user = AsyncMock(side_effect=RuntimeError("boom"))
        interaction = FakeInteraction(user=SimpleNamespace(id=31), channel_id=20)
        self.config.public_report_channel_ids = frozenset({20})
        with self.assertLogs("flock_cctv.commands", level="ERROR") as logs:
            await self.command("add", "track").callback(interaction, _user(35, "Nick"))
        self.assertIn("Slash command flock track add failed", logs.output[0])
        self.assertEqual(interaction.followup.sent[0]["content"], commands_module._FAILURE_TEXT)
        self.assertTrue(interaction.followup.sent[0]["ephemeral"])

    async def test_track_remove_accepts_ids_mentions_and_departed_members(self):
        self.config.public_report_channel_ids = frozenset({20})
        _fake_guild(self.bot)
        self.bot.fetch_user = AsyncMock(
            return_value=SimpleNamespace(display_name="Former", name="left_server")
        )
        self.bot.tracker.untrack_user = AsyncMock(return_value=True)
        remove = self.command("remove", "track")
        for actor, raw in ((31, "35"), (33, "<@!35>")):
            interaction = FakeInteraction(channel_id=20, user=SimpleNamespace(id=actor))
            await remove.callback(interaction, raw)
            sent = interaction.followup.sent[0]
            self.assertTrue(sent["ephemeral"])
            self.assertEqual(
                sent["content"],
                "Former (@left\\_server) — 35 is no longer tracked. Their recorded history is kept.",
            )
        self.assertEqual(
            self.bot.tracker.untrack_user.await_args_list,
            [call(35, 31), call(35, 33)],
        )

        self.bot.fetch_user.side_effect = TimeoutError
        unknown = FakeInteraction(user=SimpleNamespace(id=31))
        await remove.callback(unknown, "36")
        self.assertEqual(
            unknown.followup.sent[0]["content"],
            "Unknown user — 36 is no longer tracked. Their recorded history is kept.",
        )

        self.bot.tracker.untrack_user.return_value = False
        not_tracked = FakeInteraction(user=SimpleNamespace(id=31))
        await remove.callback(not_tracked, "37")
        self.assertEqual(
            not_tracked.followup.sent[0]["content"], "Unknown user — 37 isn't currently tracked."
        )

    async def test_track_remove_rejects_bad_ids_and_non_admins(self):
        self.bot.tracker.untrack_user = AsyncMock(return_value=True)
        remove = self.command("remove", "track")
        invalid = FakeInteraction(user=SimpleNamespace(id=31))
        await remove.callback(invalid, "not an id")
        self.assertIn("positive Discord user ID", invalid.response.sent[0]["content"])
        zero = FakeInteraction(user=SimpleNamespace(id=31))
        await remove.callback(zero, "0")
        self.assertIn("positive Discord user ID", zero.response.sent[0]["content"])
        for actor, manage in ((32, False), (30, False), (34, True)):
            denied = FakeInteraction(
                user=SimpleNamespace(id=actor, guild_permissions=SimpleNamespace(manage_guild=manage))
            )
            await remove.callback(denied, "35")
            self.assertIn("Only configured tracker admins", denied.response.sent[0]["content"])
            self.assertTrue(denied.response.sent[0]["ephemeral"])
        wrong = FakeInteraction(guild_id=99, user=SimpleNamespace(id=31))
        await remove.callback(wrong, "35")
        self.assertIn("configured server", wrong.response.sent[0]["content"])
        self.bot.tracker.untrack_user.assert_not_awaited()

    async def test_track_list_is_private_for_anyone_and_shows_current_then_former_with_history(self):
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.tracked = [
            _row(30), _row(41), _row(42, active=False, updated=1_700_086_400.0),
            _row(43, active=False), _row(44, active=False),
        ]
        # Only the people with recorded activity count as having history.
        self.bot.store.ranking_rows = [
            _rank(30), _rank(41, messages=3), _rank(42, voice=60.0, tracked=False),
            _rank(44, days=0, tracked=False),
        ]
        _fake_guild(self.bot, {
            30: _user(30, "Leland"), 41: _user(41, "Ana *x*", username="ana"),
            42: _user(42, "Bob"), 43: _user(43, "Never"), 44: _user(44, "Empty"),
        })
        interaction = FakeInteraction(channel_id=20, user=SimpleNamespace(id=32))
        await self.command("list", "track").callback(interaction)
        sent = interaction.followup.sent[0]
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(
            sent["content"].splitlines(),
            [
                "**Tracked people (2)**",
                "Leland (@leland) — 30 (since Nov 14, 2023)",
                "Ana \\*x\\* (@ana) — 41 (since Nov 14, 2023)",
                "Formerly tracked (history kept):",
                "Bob (@bob) — 42 (stopped Nov 15, 2023)",
            ],
        )
        self.assertEqual(self.bot.store.ranking_calls, ["all"])

    async def test_track_list_when_empty_and_when_too_long(self):
        self.bot.store.tracked = []
        _fake_guild(self.bot)
        interaction = FakeInteraction()
        await self.command("list", "track").callback(interaction)
        self.assertEqual(
            interaction.followup.sent[0]["content"].splitlines(),
            ["**Tracked people (0)**", "Nobody is tracked yet. An admin can add people with `/flock track add`."],
        )

        count = 120
        self.bot.store.tracked = [_row(1000 + number) for number in range(count)]
        _fake_guild(
            self.bot,
            {1000 + number: _user(1000 + number, f"Person number {number}") for number in range(count)},
        )
        long = FakeInteraction()
        await self.command("list", "track").callback(long)
        content = long.followup.sent[0]["content"]
        self.assertLess(len(content), 2000)
        shown = content.count("(since ")
        self.assertEqual(content.splitlines()[-1], f"…and {count - shown:,} more people")
        self.assertGreater(shown, 10)

    async def test_track_list_failure_is_safe(self):
        async def broken():
            raise RuntimeError("boom")

        self.bot.store.tracked_users = broken
        interaction = FakeInteraction()
        with self.assertLogs("flock_cctv.commands", level="ERROR") as logs:
            await self.command("list", "track").callback(interaction)
        self.assertIn("Slash command flock track list failed", logs.output[0])
        self.assertEqual(interaction.followup.sent[0]["content"], commands_module._FAILURE_TEXT)

    # -- /flock delete-data [user] ----------------------------------------------

    async def test_per_person_delete_confirms_by_name_and_deletes_only_that_person(self):
        self.bot.tracker.delete_user_data = AsyncMock(return_value=True)
        self.bot.tracker.delete_data = AsyncMock()
        command = self.command("delete-data")
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await command.callback(interaction, user=_user(41, "Ana *x*"))
        sent = interaction.response.sent[0]
        self.assertTrue(sent["ephemeral"])
        self.assertIn("**Ana \\*x\\***", sent["content"])
        self.assertIn("only", sent["content"])
        self.assertIn("Collection for everyone else continues", sent["content"])
        self.assertNotIn("pauses collection", sent["content"])
        view = sent["view"]
        self.assertEqual(view.target_id, 41)
        self.bot.tracker.delete_user_data.assert_not_awaited()

        confirmation = FakeInteraction(user=SimpleNamespace(id=31))
        await view.children[0].callback(confirmation)
        self.bot.tracker.delete_user_data.assert_awaited_once_with(41, 31)
        self.bot.tracker.delete_data.assert_not_awaited()
        edited = confirmation.edited[0]
        self.assertIsNone(edited["view"])
        self.assertIn("Ana \\*x\\*'s statistics and managed local backups were deleted", edited["content"])
        self.assertIn("Collection for everyone else continues", edited["content"])

        # Pressing the confirmation again does nothing more.
        again = FakeInteraction(user=SimpleNamespace(id=31))
        await view.children[0].callback(again)
        self.assertIn("expired or finished", again.response.sent[0]["content"])
        self.bot.tracker.delete_user_data.assert_awaited_once()

    async def test_per_person_delete_by_id_works_for_departed_members(self):
        self.bot.tracker.delete_user_data = AsyncMock(return_value=True)
        self.bot.get_user = lambda user_id: None
        command = self.command("delete-data")
        for raw in ("77", "<@77>", "<@!77>"):
            interaction = FakeInteraction(user=SimpleNamespace(id=31))
            interaction.guild = SimpleNamespace(get_member=lambda user_id: None)
            await command.callback(interaction, user_id=raw)
            sent = interaction.response.sent[0]
            self.assertTrue(sent["ephemeral"])
            self.assertIn("only **User 77**", sent["content"])
            self.assertEqual(sent["view"].target_id, 77)
        confirmation = FakeInteraction(user=SimpleNamespace(id=31))
        await sent["view"].children[0].callback(confirmation)
        self.bot.tracker.delete_user_data.assert_awaited_once_with(77, 31)

        # A cached name is used when the member is still around.
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        interaction.guild = SimpleNamespace(
            get_member=lambda user_id: SimpleNamespace(display_name="Ana", bot=False)
        )
        await command.callback(interaction, user_id="41")
        self.assertIn("only **Ana**", interaction.response.sent[0]["content"])

    async def test_per_person_delete_by_id_rejects_bad_input(self):
        self.bot.tracker.delete_user_data = AsyncMock()
        command = self.command("delete-data")
        both = FakeInteraction(user=SimpleNamespace(id=31))
        await command.callback(both, user=_user(41, "Ana"), user_id="41")
        self.assertIn("either `user` or `user_id`", both.response.sent[0]["content"])
        garbage = FakeInteraction(user=SimpleNamespace(id=31))
        await command.callback(garbage, user_id="Ana")
        self.assertIn("user ID or mention", garbage.response.sent[0]["content"])
        robot = FakeInteraction(user=SimpleNamespace(id=31))
        robot.guild = SimpleNamespace(get_member=lambda user_id: SimpleNamespace(display_name="Bot", bot=True))
        await command.callback(robot, user_id="88")
        self.assertEqual(robot.response.sent[0]["content"], "Bots aren't tracked.")
        for interaction in (both, garbage, robot):
            self.assertTrue(interaction.response.sent[0]["ephemeral"])
            self.assertNotIn("view", interaction.response.sent[0])
        self.bot.tracker.delete_user_data.assert_not_awaited()

        # Only tracker admins may delete by ID.
        outsider = FakeInteraction(user=SimpleNamespace(id=30))
        await command.callback(outsider, user_id="41")
        self.assertNotIn("view", outsider.response.sent[0])

    async def test_reports_say_busy_when_the_store_does_not_answer_in_time(self):
        async def slow():
            await asyncio.sleep(1)
            return []

        self.bot.store.tracked_users = slow
        interaction = FakeInteraction(user=SimpleNamespace(id=41))
        with patch.object(commands_module, "_LOOKUP_TIMEOUT", 0.01):
            await self.command("stats").callback(interaction)
        sent = interaction.response.sent[0]
        self.assertTrue(sent["ephemeral"])
        self.assertIn("busy", sent["content"])
        self.assertFalse(interaction.response.deferred)

    async def test_controls_say_busy_before_acknowledgement_when_admin_lookup_stalls(self):
        async def slow_override(user_id):
            await asyncio.sleep(1)
            return True

        self.bot.store.admin_override = slow_override
        self.bot.tracker.pause = AsyncMock()
        interaction = FakeInteraction(user=SimpleNamespace(id=33))
        with patch.object(commands_module, "_LOOKUP_TIMEOUT", 0.01):
            await self.command("pause").callback(interaction)
        self.assertTrue(interaction.response.sent[0]["ephemeral"])
        self.assertIn("busy", interaction.response.sent[0]["content"])
        self.assertFalse(interaction.response.deferred)
        self.bot.tracker.pause.assert_not_awaited()

    async def test_delete_confirmation_says_busy_when_admin_lookup_stalls(self):
        async def slow_override(user_id):
            await asyncio.sleep(1)
            return True

        self.bot.store.admin_override = slow_override
        self.bot.tracker.delete_data = AsyncMock()
        view = DeleteDataConfirmation(self.bot, invoker_id=33)
        interaction = FakeInteraction(user=SimpleNamespace(id=33))
        with patch.object(commands_module, "_LOOKUP_TIMEOUT", 0.01):
            await view.children[0].callback(interaction)
        self.assertIn("busy", interaction.response.sent[0]["content"])
        self.assertTrue(interaction.response.sent[0]["ephemeral"])
        self.assertFalse(view.is_finished())
        self.bot.tracker.delete_data.assert_not_awaited()

    async def test_permission_lookup_failure_is_logged_and_replies_privately(self):
        self.bot.store.admin_override = AsyncMock(side_effect=RuntimeError("synthetic failure"))
        self.bot.tracker.pause = AsyncMock()
        interaction = FakeInteraction(user=SimpleNamespace(id=33))
        with self.assertLogs("flock_cctv.commands", level="ERROR"):
            await self.command("pause").callback(interaction)
        self.assertEqual(interaction.response.sent[0]["content"], commands_module._FAILURE_TEXT)
        self.assertTrue(interaction.response.sent[0]["ephemeral"])
        self.bot.tracker.pause.assert_not_awaited()

    async def test_cancel_during_deletion_reports_processing_instead_of_cancelled(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delete_data(actor_id):
            started.set()
            await release.wait()

        self.bot.tracker.delete_data = AsyncMock(side_effect=delete_data)
        view = DeleteDataConfirmation(self.bot, invoker_id=31)
        confirmation = FakeInteraction(user=SimpleNamespace(id=31))
        task = asyncio.create_task(view.children[0].callback(confirmation))
        await started.wait()
        try:
            cancellation = FakeInteraction(user=SimpleNamespace(id=31))
            await view.children[1].callback(cancellation)
            self.assertIn("already being handled", cancellation.response.sent[0]["content"])
            self.assertTrue(cancellation.response.sent[0]["ephemeral"])
            self.assertFalse(view.is_finished())
        finally:
            release.set()
            await task

    async def test_cancel_while_confirmation_checks_access_prevents_deletion(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def admin_override(user_id):
            started.set()
            await release.wait()
            return True

        self.bot.store.admin_override = admin_override
        self.bot.tracker.delete_data = AsyncMock()
        view = DeleteDataConfirmation(self.bot, invoker_id=33)
        confirmation = FakeInteraction(user=SimpleNamespace(id=33))
        task = asyncio.create_task(view.children[0].callback(confirmation))
        await started.wait()
        cancellation = FakeInteraction(user=SimpleNamespace(id=33))
        await view.children[1].callback(cancellation)
        release.set()
        await task
        self.assertEqual(cancellation.response.sent[0]["content"], "Data deletion cancelled.")
        self.bot.tracker.delete_data.assert_not_awaited()

    def test_trend_title_with_maximum_width_name_stays_inside_canvas(self):
        png = commands_module._bar_panels_png(
            "W" * 48 + "'s average day of the week — the last 7 days",
            ["Mon"], [("Messages", [1.0], "#000000", "count")],
        )
        with Image.open(BytesIO(png)) as chart:
            title_band = chart.convert("L").crop((0, 0, chart.width, 90))
            self.assertEqual(min(title_band.crop((chart.width - 30, 0, chart.width, 90)).getdata()), 255)
            self.assertLess(min(title_band.crop((36, 20, 400, 80)).getdata()), 100)

    async def test_deleted_companions_are_named_but_never_ranked(self):
        self.bot.get_guild = lambda guild_id: SimpleNamespace(
            get_member=lambda member_id: SimpleNamespace(display_name=f"P{member_id}")
        )
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            permissions_for=lambda viewer: SimpleNamespace(view_channel=True)
        )
        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": -2, "seconds": 900.0, "full_seconds": 900.0},
            {"channel_id": 40, "member_id": 41, "seconds": 300.0, "full_seconds": 300.0},
        ]
        private = FakeInteraction(channel_id=21)
        board = await _leaderboard_text(self.bot, private, PERSON, "all")
        self.assertIn("🥇 **P41** — 5m", board)
        self.assertNotIn("Deleted person", board)
        records = await _records_text(self.bot, private, PERSON)
        self.assertIn("Top voice companion: **P41**", records)
        content, _ = await _company_report(self.bot, private, PERSON, "all")
        self.assertIn("**Deleted person** — 15m", content)

    async def test_global_delete_still_confirms_for_everyone_and_calls_delete_data(self):
        self.bot.tracker.delete_user_data = AsyncMock()
        self.bot.tracker.delete_data = AsyncMock()
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await self.command("delete-data").callback(interaction)
        sent = interaction.response.sent[0]
        self.assertTrue(sent["ephemeral"])
        self.assertIn("everyone", sent["content"])
        self.assertIn("pauses collection", sent["content"])
        view = sent["view"]
        self.assertIsNone(view.target_id)
        confirmation = FakeInteraction(user=SimpleNamespace(id=31))
        await view.children[0].callback(confirmation)
        self.bot.tracker.delete_data.assert_awaited_once_with(actor_id=31)
        self.bot.tracker.delete_user_data.assert_not_awaited()
        self.assertIn("Collection remains paused", confirmation.edited[0]["content"])
        self.assertIsNone(confirmation.edited[0]["view"])

    async def test_per_person_delete_with_nothing_recorded_and_failures(self):
        self.bot.tracker.delete_user_data = AsyncMock(return_value=False)
        view = DeleteDataConfirmation(self.bot, invoker_id=31, target_id=41, target_name="Ana")
        nothing = FakeInteraction(user=SimpleNamespace(id=31))
        await view.children[0].callback(nothing)
        self.assertEqual(nothing.edited[0]["content"], "No statistics were recorded for Ana.")

        self.bot.tracker.delete_user_data = AsyncMock(side_effect=RuntimeError("boom"))
        retry = DeleteDataConfirmation(self.bot, invoker_id=31, target_id=41, target_name="Ana")
        failed = FakeInteraction(user=SimpleNamespace(id=31))
        with self.assertLogs("flock_cctv.commands", level="ERROR"):
            await retry.children[0].callback(failed)
        self.assertIn("could not delete the data", failed.edited[0]["content"])
        self.assertIs(failed.edited[0]["view"], retry)
        self.bot.tracker.delete_user_data = AsyncMock(return_value=True)
        retried = FakeInteraction(user=SimpleNamespace(id=31))
        await retry.children[0].callback(retried)
        self.bot.tracker.delete_user_data.assert_awaited_once_with(41, 31)

    async def test_per_person_delete_confirmation_is_restricted_rechecked_and_expires(self):
        self.bot.tracker.delete_user_data = AsyncMock(return_value=True)
        view = DeleteDataConfirmation(self.bot, invoker_id=33, target_id=41, target_name="Ana")
        stranger = FakeInteraction(user=SimpleNamespace(id=31))
        await view.children[0].callback(stranger)
        self.assertIn("Only the person", stranger.response.sent[0]["content"])

        self.bot.store.admin_decisions[33] = False  # access revoked while the prompt was open
        revoked = FakeInteraction(user=SimpleNamespace(id=33))
        await view.children[0].callback(revoked)
        self.assertIn("Only configured tracker admins", revoked.response.sent[0]["content"])

        self.bot.store.admin_decisions[33] = True
        wrong_guild = FakeInteraction(guild_id=99, user=SimpleNamespace(id=33))
        await view.children[0].callback(wrong_guild)
        self.assertIn("configured server", wrong_guild.response.sent[0]["content"])

        await view.on_timeout()
        self.assertTrue(all(item.disabled for item in view.children))
        expired = FakeInteraction(user=SimpleNamespace(id=33))
        await view.children[0].callback(expired)
        self.assertIn("expired or finished", expired.response.sent[0]["content"])
        self.bot.tracker.delete_user_data.assert_not_awaited()

    async def test_delete_confirmation_cancel_is_restricted_and_deletes_nothing(self):
        self.bot.tracker.delete_user_data = AsyncMock()
        self.bot.tracker.delete_data = AsyncMock()
        view = DeleteDataConfirmation(self.bot, invoker_id=31, target_id=41, target_name="Ana")
        stranger = FakeInteraction(user=SimpleNamespace(id=33))
        await view.children[1].callback(stranger)
        self.assertIn("Only the person", stranger.response.sent[0]["content"])
        cancel = FakeInteraction(user=SimpleNamespace(id=31))
        await view.children[1].callback(cancel)
        self.assertEqual(cancel.response.sent[0]["content"], "Data deletion cancelled.")
        self.assertTrue(view.is_finished())
        self.bot.tracker.delete_user_data.assert_not_awaited()
        self.bot.tracker.delete_data.assert_not_awaited()

    async def test_delete_data_for_a_bot_is_refused_privately(self):
        self.bot.tracker.delete_user_data = AsyncMock()
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await self.command("delete-data").callback(interaction, user=_user(900, "Robo", bot=True))
        self.assertEqual(interaction.response.sent[0]["content"], "Bots aren't tracked.")
        self.assertTrue(interaction.response.sent[0]["ephemeral"])
        self.assertNotIn("view", interaction.response.sent[0])

    # -- Admins and Leland legacy ----------------------------------------------

    async def test_admin_add_rejects_leland_only_when_configured(self):
        add = self.command("add", "admin")
        leland = _user(30, "Leland")
        denied = FakeInteraction(user=SimpleNamespace(id=31))
        await add.callback(denied, leland)
        self.assertIn("Choose a human member", denied.response.sent[0]["content"])
        self.assertNotIn(30, self.bot.store.admin_decisions)

        self.config.leland_user_id = None
        allowed = FakeInteraction(user=SimpleNamespace(id=31))
        await add.callback(allowed, leland)
        self.assertTrue(self.bot.store.admin_decisions[30])

    async def test_leland_cannot_control_only_when_configured(self):
        self.config.admin_user_ids = frozenset({30, 33})
        self.assertFalse(await _can_control(FakeInteraction(user=SimpleNamespace(id=30)), self.bot))
        self.config.leland_user_id = None
        self.assertTrue(await _can_control(FakeInteraction(user=SimpleNamespace(id=30)), self.bot))
        self.assertFalse(await _can_control(FakeInteraction(user=SimpleNamespace(id=32)), self.bot))

    async def test_leland_toggles_reply_privately_when_not_configured(self):
        self.config.leland_user_id = None
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.set_evil_mode = AsyncMock()
        self.bot.store.set_reaction_mode = AsyncMock()
        for name in ("evil-mode", "reaction-mode"):
            command = self.command(name)
            with self.subTest(command=name):
                owner = FakeInteraction(channel_id=20, user=SimpleNamespace(id=31))
                await command.callback(owner, "on")
                sent = owner.response.sent[0]
                self.assertEqual(sent["content"], "Leland mode isn't configured.")
                self.assertTrue(sent["ephemeral"])
                self.assertFalse(owner.response.deferred)
                # Permission is still checked first.
                member = FakeInteraction(channel_id=20, user=SimpleNamespace(id=32))
                await command.callback(member, "on")
                self.assertIn("Only configured tracker admins", member.response.sent[0]["content"])
        self.bot.store.set_evil_mode.assert_not_awaited()
        self.bot.store.set_reaction_mode.assert_not_awaited()

    # -- help and about ---------------------------------------------------------

    async def test_help_lists_every_command_and_leland_ones_only_when_configured(self):
        text = commands_module._help_text(self.bot)
        for expected in (
            "/flock stats", "/flock records", "/flock where", "/flock company", "/flock leaderboard",
            "/flock trends", "/flock online", "/flock roast", "/flock top", "/flock track list",
            "/flock track add", "/flock help", "/flock about", "/flock version", "/flock update",
            "/flock pause", "/flock resume", "/flock delete-data", "/flock admin add",
            "optional `user`", "/flock evil-mode", "/flock reaction-mode", "America/Costa_Rica",
            "**Flock commands**",
        ):
            self.assertIn(expected, text)
        for banned in ("/leland", "tldr", "TL;DR", "Leland Tracker"):
            self.assertNotIn(banned, text)
        self.config.leland_user_id = None
        text = commands_module._help_text(self.bot)
        self.assertNotIn("evil-mode", text)
        self.assertNotIn("reaction-mode", text)
        self.assertNotIn("Leland", text)
        self.assertIn("/flock top", text)
        # A reply must fit Discord's 2000 character limit, even with a long timezone name.
        for leland_id in (30, None):
            self.config.leland_user_id = leland_id
            self.config.timezone = "America/Argentina/ComodRivadavia"
            self.assertLessEqual(len(commands_module._help_text(self.bot)), 2000)

    async def test_help_command_follows_report_visibility(self):
        self.config.public_report_channel_ids = frozenset({20})
        command = self.command("help")
        public = FakeInteraction(channel_id=20)
        await command.callback(public)
        self.assertFalse(public.followup.sent[0]["ephemeral"])
        private = FakeInteraction(channel_id=21)
        await command.callback(private)
        self.assertTrue(private.followup.sent[0]["ephemeral"])

    async def test_about_shows_tracked_people_and_leland_modes_only_when_configured(self):
        report = await _status_text(self.bot)
        self.assertIn("Tracked people: **2**", report)  # 30 and 41; 42 is no longer tracked
        self.assertIn("Evil Leland mode: **off**", report)
        self.assertIn("Reaction mode: **off**", report)
        self.config.leland_user_id = None
        report = await _status_text(self.bot)
        self.assertIn("Tracked people: **2**", report)
        self.assertNotIn("Leland", report)
        self.assertNotIn("Reaction mode", report)
        self.assertIn("Message collector: **enabled**; voice collector: **enabled**", report)
        self.assertIn("Last checkpoint:", report)
        self.assertIn("Recorded coverage gaps: **2m**", report)

    async def test_about_survives_unreadable_coverage_totals(self):
        async def broken(now):
            raise RuntimeError("boom")

        self.bot.store.coverage_gap_seconds = broken
        with self.assertLogs("flock_cctv.commands", level="ERROR"):
            report = await _status_text(self.bot)
        self.assertIn("Coverage totals are temporarily unavailable", report)
        self.assertIn("Tracked people: **2**", report)

    async def test_about_survives_an_unreadable_tracked_list(self):
        async def broken():
            raise RuntimeError("boom")

        self.bot.store.tracked_users = broken
        with self.assertLogs("flock_cctv.commands", level="ERROR"):
            report = await _status_text(self.bot)
        self.assertIn("tracked people count is temporarily unavailable", report)
        self.assertIn("Collection: **running**", report)


if __name__ == "__main__":
    unittest.main()
