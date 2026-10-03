import asyncio
from io import BytesIO
import inspect
import unittest
from unittest.mock import AsyncMock, patch
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


class FakeStore:
    def __init__(self):
        self.deleted = []
        self.last_voice_result = None
        self.company_rows = []
        self.trend_rows = []
        self.company_rows_daily = []
        self.message_times_result = {"times": [], "since": 0.0, "period_start": 0.0}
        self.voice_hours_result = {"hours": [0.0] * 24, "since": 0.0, "period_start": 0.0}
        self.comparison = {"current": {}, "previous": None, "reason": "all"}
        self.admin_decisions = {}

    async def admin_override(self, user_id):
        return self.admin_decisions.get(user_id)

    async def admin_overrides(self):
        return dict(self.admin_decisions)

    async def set_admin_override(self, user_id, enabled):
        self.admin_decisions[user_id] = enabled

    async def company_totals(self, period, now, *, include_live=True):
        self.include_live = include_live
        return self.company_rows

    async def daily_trend(self, period, now, *, include_live=True):
        self.include_live = include_live
        self.trend_period = period
        return self.trend_rows

    async def message_times(self, period, now):
        return self.message_times_result

    async def voice_hours(self, period, now, *, include_live=True):
        return self.voice_hours_result

    async def period_comparison(self, period, now, *, include_live=True):
        return self.comparison

    async def company_daily(self, period, now, *, include_live=True):
        return self.company_rows_daily

    async def last_voice(self, now, *, include_live=True):
        self.include_live = include_live
        return self.last_voice_result

    async def stats(self, period, now, *, include_live=True):
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

    async def records(self, now, *, include_live=True):
        self.include_live = include_live
        return {}

    async def state(self):
        return {"paused": False, "evil_mode": False, "paused_by": None, "tracking_since": 1_700_000_000, "last_checkpoint": 1_700_000_000}


class CommandsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            guild_id=10,
            output_channel_id=None,
            public_report_channel_ids=frozenset(),
            target_user_id=30,
            owner_user_id=31,
            admin_user_ids=frozenset({33}),
            timezone="America/Costa_Rica",
        )
        self.bot = SimpleNamespace(config=self.config, tree=FakeTree(), store=FakeStore())
        self.bot.tracker = SimpleNamespace(connected=True, guild_is_available=True, last_error=None)
        self.bot.get_channel = lambda channel_id: None

    def test_registers_single_guild_scoped_group_and_default_week(self):
        register_commands(self.bot)
        commands = {group.name: (group, guild) for group, guild in self.bot.tree.commands}
        self.assertEqual(set(commands), {"leland"})
        for group, guild in commands.values():
            self.assertEqual(guild.id, self.config.guild_id)
        leland = commands["leland"][0]
        self.assertEqual(
            {command.name for command in leland.commands},
            {
                "stats", "records", "roast", "help", "where", "company", "leaderboard", "online",
                "trends", "about", "version", "update", "pause", "resume", "delete-data", "evil-mode", "reaction-mode", "admin",
            },
        )
        admin = next(command for command in leland.commands if command.name == "admin")
        self.assertEqual({command.name for command in admin.commands}, {"add", "remove", "list"})
        stats = next(command for command in leland.commands if command.name == "stats")
        roast = next(command for command in leland.commands if command.name == "roast")
        self.assertEqual(inspect.signature(stats.callback).parameters["period"].default, "week")
        self.assertEqual(inspect.signature(roast.callback).parameters["period"].default, "week")
        trends = next(command for command in leland.commands if command.name == "trends")
        self.assertEqual(inspect.signature(trends.callback).parameters["period"].default, "last7")
        self.assertEqual(inspect.signature(trends.callback).parameters["kind"].default, "daily")
        leaderboard = next(command for command in leland.commands if command.name == "leaderboard")
        self.assertEqual(inspect.signature(leaderboard.callback).parameters["period"].default, "all")

    async def test_records_text_formats_dates_and_shows_live_visit(self):
        async def records(now, *, include_live=True):
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
        content = await _records_text(self.bot, interaction)
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
        content = await _records_text(self.bot, FakeInteraction())
        self.assertIn("No personal records have been recorded yet.", content)
        self.assertNotIn("Top voice companion", content)
        self.assertTrue(self.bot.store.include_live)

    async def test_records_text_live_visit_alone_and_unreliable_collection(self):
        async def records(now, *, include_live=True):
            self.bot.store.include_live = include_live
            return {"current_visit_seconds": 300.0, "current_visit_complete_start": True}

        self.bot.store.records = records
        content = await _records_text(self.bot, FakeInteraction())
        lines = content.splitlines()
        self.assertEqual(lines[-2], "No personal records have been recorded yet.")
        self.assertEqual(
            lines[-1],
            "Current voice visit so far: **5m** (it can set the record once it ends).",
        )
        self.bot.tracker.connected = False
        await _records_text(self.bot, FakeInteraction())
        self.assertFalse(self.bot.store.include_live)

    def test_company_pie_contains_large_visible_legend_text(self):
        png = _pie_png([("Alice 🎮", 90), ("Alone", 30)])
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
        content, png = await _trend_report(self.bot, FakeInteraction(), "week", "daily")
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
        content, png = await _trend_report(self.bot, FakeInteraction(), "all", "daily")
        self.assertIn("groups days by week", content)
        self.assertIsNotNone(png)

        self.bot.store.trend_rows = [
            {"day": "2025-03-03", "messages": 2, "voice_seconds": 0.0, "voice_visits": 0},
            {"day": "2025-03-10", "messages": 6, "voice_seconds": 0.0, "voice_visits": 0},
            {"day": "2025-03-14", "messages": 3, "voice_seconds": 7200.0, "voice_visits": 1},
            # An unwatched quiet Monday is unknown and must not dilute the average.
            {"day": "2025-03-17", "messages": 0, "voice_seconds": 0.0, "voice_visits": 0, "watched": False},
        ]
        content, png = await _trend_report(self.bot, FakeInteraction(), "month", "weekdays")
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
            content, png = await _trend_report(self.bot, FakeInteraction(), "week", kind)
            self.assertIn(expected, content)
            self.assertIsNone(png)

        self.bot.store.comparison = {
            "current": {"start": 100.0, "end": 200.0, "messages": 3, "voice_seconds": 0.0,
                        "voice_visits": 0, "watched_seconds": 100.0},
            "previous": {"start": 0.0, "end": 100.0, "messages": 2, "voice_seconds": 0.0,
                         "voice_visits": 0, "watched_seconds": 50.0},
            "reason": None,
        }
        content, _ = await _trend_report(self.bot, FakeInteraction(), "last7", "compare")
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
        content, png = await _trend_report(self.bot, FakeInteraction(), "all", "hours")
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
        content, png = await _trend_report(self.bot, FakeInteraction(), "week", "compare")
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
            content, png = await _trend_report(self.bot, FakeInteraction(), "week", "compare")
            self.assertIn(expected, content)
            self.assertIsNone(png)

    async def test_burst_trend_reports_biggest_average_and_rapid_share(self):
        base = 1_700_000_000.0
        times = [base + offset * 10 for offset in range(6)] + [base + 1000, base + 5000]
        self.bot.store.message_times_result = {"times": times, "since": base, "period_start": base}
        content, png = await _trend_report(self.bot, FakeInteraction(), "month", "bursts")
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
            content, png = await _trend_report(self.bot, interaction, "week", "company")
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
            content, _ = await _trend_report(self.bot, FakeInteraction(), "week", "company")
        self.assertIn("Tue 4: **Peer 49** — 20m", content)

    async def test_trends_command_attaches_chart_with_report_visibility(self):
        self.config.public_report_channel_ids = frozenset({20})
        self.bot.store.trend_rows = [
            {"day": "2025-03-03", "messages": 4, "voice_seconds": 60.0, "voice_visits": 1},
        ]
        register_commands(self.bot)
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        trends = next(command for command in leland.commands if command.name == "trends")
        public = FakeInteraction(channel_id=20)
        await trends.callback(public)
        self.assertEqual(self.bot.store.trend_period, "last7")
        sent = public.followup.sent[0]
        self.assertFalse(sent["ephemeral"])
        self.assertEqual(sent["file"].filename, "leland-trends-daily.png")
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
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        stats = next(command for command in leland.commands if command.name == "stats")
        where = next(command for command in leland.commands if command.name == "where")
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
        content, png = await _company_report(self.bot, public, "week")
        self.assertIn("Public friend", content)
        self.assertNotIn("Private friend", content)
        self.assertIn("1m", content)
        self.assertTrue(png.startswith(b"\x89PNG"))

        private = FakeInteraction(channel_id=21)
        private.guild = SimpleNamespace(default_role=role)
        content, _ = await _company_report(self.bot, private, "week")
        self.assertIn("Private friend", content)
        self.assertIn("3m", content)

        self.bot.store.company_rows = [
            {"channel_id": 40, "member_id": member_id, "seconds": 10}
            for member_id in range(50, 59)
        ] + [{"channel_id": 40, "member_id": 0, "seconds": 5}]
        content, _ = await _company_report(self.bot, public, "week")
        self.assertIn("Alone", content)
        self.assertIn("Other people", content)

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

        content = await _leaderboard_text(self.bot, public, "all")

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

        content = await _leaderboard_text(self.bot, interaction, "week")

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
        content = await _leaderboard_text(self.bot, interaction, "today")
        self.assertIn("No time with other people has been observed for today", content)

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

        content, png = await _company_report(self.bot, public, "week")

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
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        admin = next(command for command in leland.commands if command.name == "admin")
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
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        version = next(command for command in leland.commands if command.name == "version")
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await version.callback(interaction)
        sent = interaction.followup.sent[0]
        self.assertIn(flock_cctv.version_string(), sent["content"])
        self.assertTrue(sent["ephemeral"])

    async def test_about_command_replies_privately_with_status(self):
        register_commands(self.bot)
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        about = next(command for command in leland.commands if command.name == "about")
        interaction = FakeInteraction(user=SimpleNamespace(id=31))
        await about.callback(interaction)
        sent = interaction.followup.sent[0]
        self.assertIn("**About Leland Tracker**", sent["content"])
        self.assertIn(f"Version: **{flock_cctv.version_string()}**", sent["content"])
        self.assertIn("Collection: **running**", sent["content"])
        self.assertTrue(sent["ephemeral"])

    async def test_admin_add_and_remove_replies_name_the_user(self):
        register_commands(self.bot)
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        admin = next(command for command in leland.commands if command.name == "admin")
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
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        admin = next(command for command in leland.commands if command.name == "admin")
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
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        admin = next(command for command in leland.commands if command.name == "admin")
        remove = next(command for command in admin.commands if command.name == "remove")
        await remove.callback(FakeInteraction(user=SimpleNamespace(id=31)), "33")
        self.assertFalse(await _can_control(FakeInteraction(user=SimpleNamespace(id=33)), self.bot))
        view = DeleteDataConfirmation(self.bot, invoker_id=33)
        confirmation = FakeInteraction(user=SimpleNamespace(id=33))
        await view.children[0].callback(confirmation)
        self.assertIn("Only configured tracker admins", confirmation.response.sent[0]["content"])

    async def test_admin_commands_reject_invalid_targets_and_wrong_scope(self):
        register_commands(self.bot)
        leland = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        admin = next(command for command in leland.commands if command.name == "admin")
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
        group = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        self.bot.tracker.pause = AsyncMock()
        self.bot.tracker.resume = AsyncMock()
        self.bot.tracker.delete_data = AsyncMock()
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
        self.bot.tracker.pause.assert_not_awaited()
        self.bot.tracker.resume.assert_not_awaited()
        self.bot.tracker.delete_data.assert_not_awaited()

    async def test_owner_and_extra_admin_can_pause_and_resume(self):
        register_commands(self.bot)
        group = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
        self.bot.tracker.pause = AsyncMock()
        self.bot.tracker.resume = AsyncMock()
        self.bot.current_voice_channel_id = lambda: None
        self.bot.current_voice_companions = lambda: frozenset()
        for name, user_id in (("pause", 31), ("resume", 33)):
            interaction = FakeInteraction(user=SimpleNamespace(id=user_id))
            command = next(command for command in group.commands if command.name == name)
            await command.callback(interaction)
            self.assertTrue(interaction.response.deferred)
        self.bot.tracker.pause.assert_awaited_once_with(actor_id=31)
        self.bot.tracker.resume.assert_awaited_once_with(actor_id=33, voice_channel_id=None, companions=frozenset())

    async def test_update_command_requests_a_check_privately_unless_held(self):
        import json
        import tempfile
        from pathlib import Path

        register_commands(self.bot)
        group = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
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
        group = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
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
        group = next(group for group, _ in self.bot.tree.commands if group.name == "leland")
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
        report = await _stats_text(self.bot, "week")
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
        self.assertIn("Recorded coverage gaps", report)

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
            report = await _seen_text(self.bot, FakeInteraction())
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
            hidden = await _seen_text(self.bot, interaction)
            everyone_can_view = True
            visible = await _seen_text(self.bot, interaction)
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
            report = await _seen_text(self.bot, FakeInteraction())
        self.assertIn("a voice channel", report)
        self.assertNotIn("private-voice", report)
        self.assertFalse(self.bot.store.include_live)

    async def test_seen_handles_current_and_empty_observation(self):
        self.assertIn("haven't observed", await _seen_text(self.bot, FakeInteraction()))
        self.bot.store.last_voice_result = {
            "channel_id": 40, "seen_at": 1_700_003_600,
            "current": True, "observed_since": 1_700_000_000,
        }
        self.bot.get_channel = lambda channel_id: SimpleNamespace(
            name="voice", permissions_for=lambda user: SimpleNamespace(view_channel=True),
        )
        self.assertIn("currently in **#voice**", await _seen_text(self.bot, FakeInteraction()))

    async def test_online_counts_away_and_dnd_as_online(self):
        guild = SimpleNamespace(query_members=AsyncMock())
        self.bot.get_guild = lambda guild_id: guild
        for status in (discord.Status.online, discord.Status.idle, discord.Status.dnd):
            with self.subTest(status=status):
                guild.query_members.return_value = [SimpleNamespace(id=30, status=status)]
                self.assertIn("online", await _online_text(self.bot))
        guild.query_members.assert_awaited_with(user_ids=[30], presences=True, cache=False)

    async def test_online_reports_offline_and_unknown_without_guessing(self):
        guild = SimpleNamespace(query_members=AsyncMock())
        self.bot.get_guild = lambda guild_id: guild
        guild.query_members.return_value = [SimpleNamespace(id=30, status=discord.Status.offline)]
        self.assertIn("offline or invisible", await _online_text(self.bot))
        guild.query_members.return_value = []
        self.assertIn("couldn't find", await _online_text(self.bot))
        guild.query_members.side_effect = TimeoutError()
        self.assertIn("couldn't check", await _online_text(self.bot))
        self.bot.get_guild = lambda guild_id: None
        self.assertIn("server is unavailable", await _online_text(self.bot))

    async def test_failed_recovery_disables_live_totals_and_is_visible_in_reports(self):
        self.bot.tracker.collection_ready = False
        report = await _stats_text(self.bot, "week")
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

        await _execute_after_scope(interaction, self.bot, "leland resume", action, ephemeral=True)
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


VALID_SUMMARY = (
    '{"topics": ["synthetic topic"], "mood": "Calm synthetic vibe", '
    '"roast": "A gentle synthetic verdict.", "quote": "alpha text"}'
)


class FakeTldrClient:
    def __init__(self, result=VALID_SUMMARY, error=None):
        self.result = result
        self.error = error
        self.bodies = []

    async def complete(self, body):
        self.bodies.append(body)
        if self.error is not None:
            raise self.error
        return self.result

    def texts(self):
        user = self.bodies[0]["messages"][1]["content"]
        return [line[2:] for line in user.splitlines() if line.startswith("- ")]


class FakeHistoryChannel:
    def __init__(self, channel_id, messages=(), *, viewer_can_view=None, bot_perms=None, error=None):
        self.id = channel_id
        self.messages = list(messages)
        self.me = object()
        self.guild = SimpleNamespace(me=self.me)
        self.viewer_can_view = viewer_can_view or (lambda viewer: True)
        self.bot_perms = bot_perms or SimpleNamespace(view_channel=True, read_message_history=True)
        self.error = error
        self.history_calls = []

    def permissions_for(self, who):
        if who is self.me:
            return self.bot_perms
        return SimpleNamespace(view_channel=self.viewer_can_view(who))

    def history(self, **kwargs):
        self.history_calls.append(kwargs)
        channel = self

        async def generator():
            if channel.error is not None:
                raise channel.error
            for message in sorted(channel.messages, key=lambda item: item.id, reverse=True):
                yield message

        return generator()


class FakePrivateThread(FakeHistoryChannel, discord.Thread):
    def is_private(self):
        return True


def _message(message_id, content, author_id=30):
    return SimpleNamespace(id=message_id, content=content, author=SimpleNamespace(id=author_id))


class TldrStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.paused = False
        self.counts = {}
        self.rows = []
        self.latest_calls = []

    async def state(self):
        state = await super().state()
        state["paused"] = self.paused
        return state

    async def message_channel_counts(self, period, now):
        return dict(self.counts)

    async def latest_messages(self, period, now, channel_ids, limit):
        self.latest_calls.append((sorted(channel_ids), limit))
        rows = [row for row in self.rows if row["channel_id"] in set(channel_ids)]
        rows.sort(key=lambda row: (row["created_at"], row["message_id"]), reverse=True)
        return rows[:limit]


if __name__ == "__main__":
    unittest.main()
