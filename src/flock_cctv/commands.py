"""Guild-scoped slash commands for the tracker."""

from __future__ import annotations

import asyncio
from io import BytesIO
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Awaitable, Callable, TypeVar

import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

from . import update_status, version_string
from .jokes import SharedRoastCooldown, make_roast
from .stats import get_timezone, local_date
from .storage import DELETED_COMPANION_ID
from .text import clean_name as _clean_name
from .text import duration as _duration
from .text import local_time as _local_time
from .text import safe_name as _safe_name

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

PERIOD_LABELS = {
    "today": "today",
    "week": "this week",
    "last7": "the last 7 days",
    "month": "this month",
    "all": "all time",
}
PERIOD_CHOICES = [
    app_commands.Choice(name="Today", value="today"),
    app_commands.Choice(name="This week", value="week"),
    app_commands.Choice(name="This month", value="month"),
    app_commands.Choice(name="All time", value="all"),
]
TOP_METRIC_CHOICES = [
    app_commands.Choice(name="Messages", value="messages"),
    app_commands.Choice(name="Voice time", value="voice"),
    app_commands.Choice(name="Active days", value="active_days"),
]
# Trends default to a rolling window so early in the week the chart still has data.
TREND_PERIOD_CHOICES = [app_commands.Choice(name="Last 7 days", value="last7"), *PERIOD_CHOICES]
# How company reports count a minute shared with several people.
COMPANY_COUNT_CHOICES = [
    app_commands.Choice(name="Split evenly among people present", value="split"),
    app_commands.Choice(name="Full time with each person", value="full"),
]

_ROAST_COOLDOWN = SharedRoastCooldown(seconds=30)
_INTRODUCE_COOLDOWN = SharedRoastCooldown(seconds=300)
# Company member ID that stands in for people whose data was deleted.
_DELETED_COMPANION = int(DELETED_COMPANION_ID)
# Seconds a pre-acknowledgement store read may take (Discord allows three).
_LOOKUP_TIMEOUT = 2.0
_FAILURE_TEXT = "The tracker could not complete that request. The error was logged."
_BUSY_TEXT = "The tracker is busy right now. Try again in a moment."
# Lists stop short of Discord's 2,000-character message limit.
_LIST_LIMIT = 1850
_USER_OPTION = "Whose activity to show (defaults to you)"
_COMPANY_COUNT_OPTION = "How to count time shared with several people (split by default)"
# Categorical palette in fixed slot order, validated for colour-vision deficiency.
_PIE_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")


def _config(bot: Any) -> Any:
    return bot.config


def _leland_id(bot: Any) -> int | None:
    """The configured legacy Leland user, or ``None`` when Leland mode is off."""
    return getattr(_config(bot), "leland_user_id", None)


def _guild(bot: Any) -> Any:
    """The configured guild from the client cache, or ``None``."""
    return bot.get_guild(_config(bot).guild_id) if hasattr(bot, "get_guild") else None


def _period_label(period: str) -> str:
    return PERIOD_LABELS.get(period, "this period")


def _report_is_ephemeral(bot: Any, interaction: discord.Interaction) -> bool:
    config = _config(bot)
    if getattr(config, "output_channel_id", None) is not None:
        return False
    public_channels = getattr(config, "public_report_channel_ids", frozenset())
    return public_channels is not None and interaction.channel_id not in public_channels


async def _send(
    interaction: discord.Interaction,
    content: str,
    *,
    ephemeral: bool,
    view: discord.ui.View | None = None,
    file: discord.File | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "content": content,
        "ephemeral": ephemeral,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if view is not None:
        kwargs["view"] = view
    if file is not None:
        kwargs["file"] = file
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def _scope_ok(interaction: discord.Interaction, bot: Any) -> bool:
    config = _config(bot)
    if interaction.guild_id != config.guild_id:
        await _send(
            interaction,
            "These commands are available only in the configured server.",
            ephemeral=True,
        )
        return False
    output_channel_id = getattr(config, "output_channel_id", None)
    if output_channel_id is not None and interaction.channel_id != output_channel_id:
        await _send(
            interaction,
            "Use the configured tracker channel for these commands.",
            ephemeral=True,
        )
        return False
    return True


async def _can_control(interaction: discord.Interaction, bot: Any) -> bool:
    config = _config(bot)
    user_id = interaction.user.id
    if user_id == config.owner_user_id:
        return True
    if user_id == _leland_id(bot):
        return False
    override = await bot.store.admin_override(user_id)
    return user_id in config.admin_user_ids if override is None else override


async def _control_ok(
    interaction: discord.Interaction, bot: Any, *, leland: bool = False
) -> bool:
    """Apply the scope check, then require a tracker admin, replying privately on refusal.

    With ``leland``, also refuse when Leland mode isn't configured.
    """
    if not await _scope_ok(interaction, bot):
        return False
    try:
        can_control = await asyncio.wait_for(
            _can_control(interaction, bot), timeout=_LOOKUP_TIMEOUT
        )
    except TimeoutError:
        await _send(interaction, _BUSY_TEXT, ephemeral=True)
        return False
    except Exception:
        logger.exception("Tracker admin permission lookup failed")
        await _send(interaction, _FAILURE_TEXT, ephemeral=True)
        return False
    if not can_control:
        await _send(
            interaction,
            "Only configured tracker admins can change tracker settings.",
            ephemeral=True,
        )
        return False
    if leland and _leland_id(bot) is None:
        await _send(interaction, "Leland mode isn't configured.", ephemeral=True)
        return False
    return True


async def _owner_ok(interaction: discord.Interaction, bot: Any) -> bool:
    """Apply the scope check, then require the configured owner, replying privately on refusal."""
    if not await _scope_ok(interaction, bot):
        return False
    if interaction.user.id == _config(bot).owner_user_id:
        return True
    await _send(
        interaction,
        "Only the configured tracker owner can manage admins.",
        ephemeral=True,
    )
    return False


async def _cooldown_ok(
    interaction: discord.Interaction, cooldown: SharedRoastCooldown, refusal: str
) -> bool:
    """Take a slot from a shared cooldown, or reply privately with the wait."""
    remaining = await cooldown.consume()
    if remaining <= 0:
        return True
    await _send(
        interaction,
        f"{refusal} Try again in {max(1, math.ceil(remaining))} seconds.",
        ephemeral=True,
    )
    return False


def _parse_user_id(raw: str) -> int | None:
    """Read a positive Discord user ID from a plain ID or a user mention."""
    match = re.fullmatch(r"(?:<@!?(\d+)>|(\d+))", raw.strip())
    if match is None:
        return None
    user_id = int(match.group(1) or match.group(2))
    return user_id if user_id > 0 else None


async def _run(
    interaction: discord.Interaction,
    label: str,
    action: Callable[[], Awaitable[str | tuple[str, bytes | None]]],
    *,
    ephemeral: bool,
    note: str = "",
    filename: str = "chart.png",
) -> None:
    """Defer, run ``action``, and reply with its text and optional PNG chart.

    Access checks have already happened. ``note`` is appended to a successful
    reply; a failure is logged and gets the generic failure text instead.
    """
    await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    file = None
    try:
        result = await action()
        content, png = result if isinstance(result, tuple) else (result, None)
        content += note
        if png:
            file = discord.File(BytesIO(png), filename=filename)
    except Exception:
        logger.exception("Slash command %s failed", label)
        content, file = _FAILURE_TEXT, None
    await _send(interaction, content, ephemeral=ephemeral, file=file)


def _collection_problem(bot: Any) -> str | None:
    """Why live collection can't be trusted right now, or ``None`` when it can.

    Returns ``disconnected``, ``guild_unavailable``, or ``recovering``.
    """
    tracker = bot.tracker
    if not getattr(tracker, "connected", False):
        return "disconnected"
    if not getattr(tracker, "guild_is_available", False):
        return "guild_unavailable"
    if not getattr(tracker, "collection_ready", False):
        return "recovering"
    return None


def _collection_reliable(bot: Any) -> bool:
    return _collection_problem(bot) is None


_STATS_COLLECTION_NOTES = {
    "disconnected": "Collection is currently unavailable because the bot is disconnected.",
    "guild_unavailable": "Collection is currently unavailable because the configured server is unavailable.",
    "recovering": "Collection is recovering. Voice time includes only saved observations until recovery finishes.",
}


async def _stats_text(bot: Any, person: _Person, period: str) -> str:
    now = time.time()
    result = await _read_stats(bot, person.user_id, period, now)
    timezone = bot.config.timezone
    label = _period_label(period)
    tracking_since = _local_time(result["tracking_since"], timezone, date_only=True)
    lines = [f"**The {person.safe} Report — {label}**"]
    if not any(
        (
            int(result.get("messages", 0) or 0) > 0,
            float(result.get("voice_seconds", 0) or 0) > 0,
            int(result.get("voice_visits", 0) or 0) > 0,
            int(result.get("active_days", 0) or 0) > 0,
        )
    ):
        lines.append("No activity has been recorded for this period.")
    else:
        lines.extend(
            [
                f"Messages: **{int(result['messages']):,}**",
                f"Observed voice time: **{_duration(result['voice_seconds'])}**",
                f"Observed voice visits: **{int(result['voice_visits']):,}**",
                f"Active days: **{int(result['active_days']):,}**",
            ]
        )
    lines.append(f"Tracking since {tracking_since} ({timezone}).")
    gaps = float(result.get("gap_seconds", 0) or 0)
    if gaps > 0:
        lines.append(f"Missing coverage during this period: **{_duration(gaps)}**.")
    if bool(result.get("paused", False)):
        lines.append("Collection is currently paused.")
    problem = _collection_problem(bot)
    if problem is not None:
        lines.append(_STATS_COLLECTION_NOTES[problem])
    return "\n".join(lines)


async def _read_stats(bot: Any, user_id: int, period: str, now: float) -> dict[str, Any]:
    return await bot.store.stats(user_id, period, now, include_live=_collection_reliable(bot))


async def _seen_text(bot: Any, interaction: discord.Interaction, person: _Person) -> str:
    now = time.time()
    observation = await bot.store.last_voice(
        person.user_id, now, include_live=_collection_reliable(bot)
    )
    if observation is None:
        return f"I haven't observed {person.safe} in a tracked voice channel yet."

    channel_id = int(observation["channel_id"])
    channel = bot.get_channel(channel_id)
    channel_label = "a voice channel"
    # A public reply must not reveal a channel name visible only to the caller.
    if channel is not None and _audience_can_view(bot, interaction)(channel_id):
        name = discord.utils.escape_markdown(str(channel.name))
        channel_label = f"**#{name}**"

    if observation["current"]:
        since = _local_time(observation["observed_since"], bot.config.timezone)
        return f"{person.safe} is currently in {channel_label}. I've observed them there since {since}."

    seen_at = float(observation["seen_at"])
    elapsed = _duration(max(0.0, now - seen_at))
    date = _local_time(seen_at, bot.config.timezone)
    return f"{person.safe} was last seen in {channel_label} **{elapsed} ago** ({date})."


def _pie_png(
    slices: list[tuple[str, float]], owner: str, details: list[str] | None = None
) -> bytes:
    """Draw a pie with a legend; ``details`` replaces each slice's share and duration line."""
    image = Image.new("RGB", (1100, 640), "#ffffff")
    draw = ImageDraw.Draw(image)
    title_font, label_font, detail_font = _font(38), _font(32), _font(24)
    title = f"{owner}'s voice company"
    while len(title) > 1 and title_font.getlength(title) > 1100 - 2 * 44:
        title = title[:-2] + "…"
    draw.text((44, 28), title, font=title_font, fill="#17212f")
    total = sum(seconds for _, seconds in slices)
    angle = -90.0
    for index, (name, seconds) in enumerate(slices):
        next_angle = angle + 360.0 * seconds / total
        draw.pieslice(
            (44, 108, 560, 624), start=angle, end=next_angle,
            fill=_PIE_COLORS[index], outline="#ffffff", width=3,
        )
        legend_y = 89 + index * 68
        draw.rectangle((610, legend_y + 5, 644, legend_y + 39), fill=_PIE_COLORS[index])
        visible_name = name
        while visible_name and label_font.getlength(visible_name) > 398:
            visible_name = visible_name[:-1]
        if visible_name != name:
            while visible_name and label_font.getlength(visible_name + "…") > 398:
                visible_name = visible_name[:-1]
            visible_name += "…"
        draw.text((660, legend_y), visible_name, font=label_font, fill="#17212f")
        detail = details[index] if details else f"{seconds / total:.1%}  ·  {_duration(seconds)}"
        draw.text((660, legend_y + 36), detail, font=detail_font, fill="#48576b")
        angle = next_angle
    return _png_bytes(image)


async def _lookup_user(bot: Any, guild: Any, user_id: int) -> Any:
    """Find a member or user from cache first, then from Discord, or ``None``."""
    member = guild.get_member(user_id) if guild is not None else None
    user = bot.get_user(user_id) if member is None and hasattr(bot, "get_user") else None
    found = member or user
    if getattr(found, "display_name", None):
        return found
    if guild is not None:
        try:
            member = await asyncio.wait_for(guild.fetch_member(user_id), timeout=5)
            if getattr(member, "display_name", None):
                return member
        except (discord.HTTPException, TimeoutError):
            pass
    if hasattr(bot, "fetch_user"):
        try:
            user = await asyncio.wait_for(bot.fetch_user(user_id), timeout=5)
            if getattr(user, "display_name", None):
                return user
        except (discord.HTTPException, TimeoutError):
            pass
    return None


async def _company_name(bot: Any, guild: Any, member_id: int) -> str:
    if member_id == 0:
        return "Alone"
    if member_id == -1:
        return "Other people"
    if member_id == _DELETED_COMPANION:
        return "Deleted person"
    found = await _lookup_user(bot, guild, member_id)
    return _clean_name(getattr(found, "display_name", None)) or f"User {member_id}"


def _user_label(found: Any, user_id: int) -> str:
    """Describe a user by display name and username, keeping the ID."""
    display = _clean_name(getattr(found, "display_name", None))
    username = _clean_name(getattr(found, "name", None))
    if username and display and display != username:
        label = f"{display} (@{username})"
    elif username:
        label = f"@{username}"
    else:
        label = display or "Unknown user"
    return f"{_safe_name(label)} — {user_id}"


async def _admin_label(bot: Any, guild: Any, user_id: int) -> str:
    return _user_label(await _lookup_user(bot, guild, user_id), user_id)


@dataclass(frozen=True)
class _Person:
    """The tracked person a report is about, resolved from the ``user`` option."""

    user_id: int
    name: str  # cleaned display name; plain text, so only fit for images
    active: bool  # currently on the tracked list
    since: float  # when they were first tracked

    @property
    def safe(self) -> str:
        """The display name escaped for Markdown and mentions."""
        return _safe_name(self.name)

    @property
    def note(self) -> str:
        """A line to add to reports about someone who is no longer tracked."""
        if self.active:
            return ""
        return f"\n{self.safe} is no longer tracked; showing recorded history."


def _person_name(user: Any, user_id: int) -> str:
    return (
        _clean_name(getattr(user, "display_name", None))
        or _clean_name(getattr(user, "name", None))
        or f"User {user_id}"
    )


async def _resolve_person(
    interaction: discord.Interaction,
    bot: Any,
    label: str,
    user: Any,
    *,
    require_active: bool = False,
) -> _Person | None:
    """Apply the scope check and look up the person a report is about.

    Replies privately and returns ``None`` for a bot, someone who was never
    tracked, or (with ``require_active``) someone no longer tracked. The reply
    is private whatever the channel, so it must happen before the report defers.
    """
    if not await _scope_ok(interaction, bot):
        return None
    target = interaction.user if user is None else user
    if getattr(target, "bot", False):
        await _send(interaction, "Bots aren't tracked.", ephemeral=True)
        return None
    user_id = int(target.id)
    name = _person_name(target, user_id)
    try:
        # This runs before the interaction is acknowledged, so a store busy with
        # a backup or compaction must not use up Discord's three seconds.
        rows = await asyncio.wait_for(bot.store.tracked_users(), timeout=_LOOKUP_TIMEOUT)
    except TimeoutError:
        await _send(interaction, _BUSY_TEXT, ephemeral=True)
        return None
    except Exception:
        logger.exception("Slash command %s failed", label)
        await _send(interaction, _FAILURE_TEXT, ephemeral=True)
        return None
    row = next((item for item in rows if int(item["user_id"]) == user_id), None)
    if row is None:
        await _send(
            interaction,
            f"{_safe_name(name)} isn't tracked. An admin can add them with `/flock track add`.",
            ephemeral=True,
        )
        return None
    if require_active and not row["active"]:
        await _send(
            interaction,
            f"{_safe_name(name)} is no longer tracked. An admin can add them again with `/flock track add`.",
            ephemeral=True,
        )
        return None
    return _Person(user_id, name, bool(row["active"]), float(row["tracking_since"]))


def _audience_can_view(bot: Any, interaction: discord.Interaction) -> Callable[[int], bool]:
    """Return a check for whether the report's audience can view a channel.

    A private reply is seen by the requester; a public one by ``@everyone``.
    A missing channel or viewer is never visible. Answers are cached per
    channel for the life of the returned check.
    """
    viewer = (
        interaction.user if _report_is_ephemeral(bot, interaction)
        else getattr(getattr(interaction, "guild", None), "default_role", None)
    )

    @lru_cache(maxsize=None)
    def can_view(channel_id: int) -> bool:
        if viewer is None:
            return False
        try:
            return bool(bot.get_channel(channel_id).permissions_for(viewer).view_channel)
        except (AttributeError, TypeError):
            return False

    return can_view


def _visible_company_rows(
    bot: Any, interaction: discord.Interaction, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep positive company rows from voice channels the report audience can view."""
    can_view = _audience_can_view(bot, interaction)
    return [
        row for row in rows
        if float(row["seconds"]) > 0 and can_view(int(row["channel_id"]))
    ]


async def _visible_company_totals(
    bot: Any, interaction: discord.Interaction, person: _Person, period: str
) -> list[dict[str, Any]]:
    """Return company totals by channel and peer from channels the audience can view."""
    rows = await bot.store.company_totals(
        person.user_id, period, time.time(), include_live=_collection_reliable(bot)
    )
    return _visible_company_rows(bot, interaction, rows)


def _seconds_by_member(rows: list[dict[str, Any]], field: str) -> dict[int, float]:
    """Sum one company field by peer (0 means alone), dropping peers without time.

    ``field`` is ``seconds`` for the even split or ``full_seconds`` for whole shared time.
    """
    seconds_by_member: dict[int, float] = {}
    for row in rows:
        seconds = float(row.get(field, 0.0))
        if seconds <= 0:
            continue
        member_id = int(row["member_id"])
        seconds_by_member[member_id] = seconds_by_member.get(member_id, 0.0) + seconds
    return seconds_by_member


async def _company_seconds(
    bot: Any, interaction: discord.Interaction, person: _Person, period: str, field: str = "seconds"
) -> dict[int, float]:
    """Return seconds by peer (0 means alone) in channels the audience can view."""
    return _seconds_by_member(await _visible_company_totals(bot, interaction, person, period), field)


def _company_field(count: str) -> str:
    """Map a company ``count`` choice to the stored field it reads."""
    return "full_seconds" if count == "full" else "seconds"


async def _company_report(
    bot: Any, interaction: discord.Interaction, person: _Person, period: str, count: str = "split"
) -> tuple[str, bytes | None]:
    rows = await _visible_company_totals(bot, interaction, person, period)
    # Split shares sum to the observed time; full time overlaps and cannot.
    observed_by_member = _seconds_by_member(rows, "seconds")
    full = count == "full"
    seconds_by_member = _seconds_by_member(rows, "full_seconds") if full else observed_by_member

    label = _period_label(period)
    if not seconds_by_member:
        return (
            f"No companion time has been observed for {person.safe} {label} in voice channels visible to this report.",
            None,
        )
    ranked_peers = sorted(
        ((member_id, seconds) for member_id, seconds in seconds_by_member.items() if member_id != 0),
        key=lambda item: (-item[1], item[0]),
    )
    limit = 6 if 0 in seconds_by_member else 7
    top = ranked_peers[:limit]
    if 0 in seconds_by_member:
        top.append((0, seconds_by_member[0]))
        top.sort(key=lambda item: (-item[1], item[0]))
    rest = len(ranked_peers) - limit
    if rest > 0:
        top.append((-1, sum(seconds for _, seconds in ranked_peers[limit:])))
    guild = _guild(bot)
    names = await asyncio.gather(*(
        _company_name(bot, guild, member_id) for member_id, _ in top
    ))
    slices = [(name, seconds) for name, (_, seconds) in zip(names, top)]
    total = sum(observed_by_member.values())
    title = f"**{person.safe}'s voice company — {label}**"
    lines = [
        f"{title} (full time with each person)" if full else title,
        f"Observed time in visible channels: **{_duration(total)}**.",
    ]
    details = []
    for index, ((member_id, _), (name, seconds)) in enumerate(zip(top, slices)):
        safe_name = _safe_name(name)
        if full and member_id == -1:
            # Overlapping time with several people has no meaningful share of the whole.
            details.append(f"{_duration(seconds)} combined")
            lines.append(
                f"{index + 1}. **{safe_name}** — {_duration(seconds)} combined across {rest} people"
            )
        elif full:
            details.append(f"{seconds / total:.1%} of time  ·  {_duration(seconds)}")
            lines.append(
                f"{index + 1}. **{safe_name}** — {_duration(seconds)} ({seconds / total:.1%} of observed time)"
            )
        else:
            lines.append(
                f"{index + 1}. **{safe_name}** — {_duration(seconds)} ({seconds / total:.1%})"
            )
    if full:
        lines.append(
            "Each person is credited with every minute they shared, so slices overlap: percentages are "
            "of observed time and the chart shows relative shares. Time alone has its own slice. "
            "Only observed time since companion tracking began is included."
        )
    else:
        lines.append(
            "Each shared minute is split evenly among the people present; time alone has its own slice. "
            "Only observed time since companion tracking began is included."
        )
    png = await _render(_pie_png, slices, person.name, details if full else None)
    return "\n".join(lines), png


_LEADERBOARD_SIZE = 10
_LEADERBOARD_MEDALS = ("🥇", "🥈", "🥉")


def _rank_label(index: int) -> str:
    """A medal for the top three places, then ``4.``, ``5.``, and so on."""
    return _LEADERBOARD_MEDALS[index] if index < len(_LEADERBOARD_MEDALS) else f"{index + 1}."


async def _leaderboard_text(
    bot: Any, interaction: discord.Interaction, person: _Person, period: str
) -> str:
    """Rank people by the whole voice time they shared with ``person``."""
    full_by_member = await _company_seconds(bot, interaction, person, period, "full_seconds")
    label = _period_label(period)
    # Time alone and time with people whose data was deleted are not ranked.
    ranked = sorted(
        ((member_id, seconds) for member_id, seconds in full_by_member.items() if member_id > 0),
        key=lambda item: (-item[1], item[0]),
    )
    if not ranked:
        return (
            f"No time with other people has been observed for {person.safe} {label} in voice channels visible to this report. "
            "Only observed time since companion tracking began is counted."
        )
    top = ranked[:_LEADERBOARD_SIZE]
    guild = _guild(bot)
    names = await asyncio.gather(*(_company_name(bot, guild, member_id) for member_id, _ in top))
    lines = [f"**{person.safe}'s voice leaderboard — {label}**"]
    for index, (name, (_, seconds)) in enumerate(zip(names, top)):
        lines.append(f"{_rank_label(index)} **{_safe_name(name)}** — {_duration(seconds)}")
    if len(ranked) > len(top):
        lines.append(f"…and {len(ranked) - len(top):,} more.")
    if full_by_member.get(0, 0.0) > 0:
        lines.append(f"Time alone (not ranked): {_duration(full_by_member[0])}.")
    lines.append(
        f"Each person gets the whole time they were in a tracked voice channel with {person.safe}, so "
        "group calls count fully for everyone."
    )
    return "\n".join(lines)


# Discord shrinks attachments to the chat width (often 400px or less), so the
# canvas stays narrow and text stays large enough to read after that shrink.
_CHART_WIDTH = 900
_CHART_MARGIN = 36
_TEXT_PRIMARY = "#0b0b0b"
_TEXT_SECONDARY = "#52514e"
_GRID = "#e6e5e1"
_BASELINE = "#b9b8b2"
_BAR_MAX_WIDTH = 56
_BAR_RADIUS = 8
_SEGMENT_GAP = 3
# Round duration steps for a time axis, in seconds.
_DURATION_STEPS = (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800)


@lru_cache(maxsize=None)
def _font(size: int) -> Any:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def _chart_fonts() -> tuple[Any, Any]:
    """Return the heading and axis/legend fonts."""
    return _font(34), _font(30)


def _draw_title(draw: ImageDraw.ImageDraw, title: str) -> None:
    """Draw the title at the largest size up to 46px that fits the canvas."""
    size = 46
    font = _font(size)
    while size > 24 and font.getlength(title) > _CHART_WIDTH - 2 * _CHART_MARGIN:
        size -= 2
        font = _font(size)
    while len(title) > 1 and font.getlength(title) > _CHART_WIDTH - 2 * _CHART_MARGIN:
        title = title[:-2] + "…"
    draw.text((_CHART_MARGIN, 24), title, font=font, fill=_TEXT_PRIMARY)


def _axis_ticks(peak: float, kind: str) -> list[float]:
    """Return two to five evenly spaced round ticks from zero that cover ``peak``.

    ``kind`` is ``count`` (whole numbers), ``average`` (fractions allowed), or
    ``duration`` (seconds, stepped in round clock units).
    """
    if peak <= 0:
        return [0.0, 3600.0 if kind == "duration" else 1.0]
    target = peak / 4
    if kind == "duration":
        step = next(
            (float(value) for value in _DURATION_STEPS if value >= target),
            math.ceil(target / 604800) * 604800.0,
        )
    else:
        magnitude = 10 ** math.floor(math.log10(target))
        step = next(multiple * magnitude for multiple in (1, 2, 2.5, 5, 10) if multiple * magnitude >= target)
        if kind == "count":
            step = max(1.0, float(math.ceil(step)))
    intervals = max(1, math.ceil(peak / step - 1e-9))
    return [index * step for index in range(intervals + 1)]


def _axis_label(value: float, kind: str) -> str:
    if kind == "duration":
        if value == 0:
            return "0"
        hours, minutes = divmod(int(round(value / 60)), 60)
        if not hours:
            return f"{minutes}m"
        return f"{hours}h" if not minutes else f"{hours}h{minutes:02d}"
    if kind == "average":
        return f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{int(round(value)):,}"


def _value_label(value: float, kind: str) -> str:
    if kind == "duration":
        return _duration(value)
    if kind == "average":
        return f"{value:.1f}"
    return f"{int(round(value)):,}"


def _draw_column(
    draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], colour: str, *, rounded: bool
) -> None:
    """Draw a column with a rounded data end and a square baseline end."""
    x0, y0, x1, y1 = box
    radius = min(_BAR_RADIUS, (x1 - x0) / 2, (y1 - y0) / 2) if rounded else 0
    if radius >= 1:
        draw.rounded_rectangle(box, radius=radius, fill=colour, corners=(True, True, False, False))
    else:
        draw.rectangle(box, fill=colour)


def _plot_frame(
    draw: ImageDraw.ImageDraw,
    font: Any,
    labels: list[str],
    ticks: list[float],
    kind: str,
    chart_top: float,
    base: float,
) -> tuple[float, float, float]:
    """Draw gridlines, y tick labels, and x labels; return ``(left, slot, scale)``."""
    tick_labels = [_axis_label(value, kind) for value in ticks]
    left = _CHART_MARGIN + max(font.getlength(label) for label in tick_labels) + 16
    right = _CHART_WIDTH - _CHART_MARGIN
    scale = (base - chart_top) / ticks[-1]
    for value, label in zip(ticks, tick_labels):
        y = base - value * scale
        draw.line((left, y, right, y), fill=_BASELINE if value == 0 else _GRID, width=2)
        draw.text((left - 14, y), label, font=font, fill=_TEXT_SECONDARY, anchor="rm")
    slot = (right - left) / max(1, len(labels))
    # Thin the x labels so neighbours never overlap.
    widest = max((font.getlength(label) for label in labels), default=0)
    step = max(1, int((widest + 16) // slot) + 1)
    for position in range(0, len(labels), step):
        # Low enough to clear the "0" tick label when the first one reaches the gutter.
        draw.text(
            (left + (position + 0.5) * slot, base + 20), labels[position],
            font=font, fill=_TEXT_SECONDARY, anchor="mt",
        )
    return left, slot, scale


def _label_peak(
    draw: ImageDraw.ImageDraw, font: Any, text: str, centre: float, top: float, left: float
) -> None:
    """Label the tallest mark just above its data end, kept inside the plot."""
    half = font.getlength(text) / 2
    centre = min(max(centre, left + half), _CHART_WIDTH - _CHART_MARGIN - half)
    draw.text((centre, top - 8), text, font=font, fill=_TEXT_PRIMARY, anchor="mb")


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


# Cached fonts are shared, and FreeType faces are not safe to use from two
# threads at once, so charts render one at a time.
_RENDER_LOCK = threading.Lock()


async def _render(draw: Callable[..., _T], /, *args: Any) -> _T:
    """Run a function that draws a chart on a worker thread, off the event loop."""

    def locked() -> _T:
        with _RENDER_LOCK:
            return draw(*args)

    return await asyncio.to_thread(locked)


def _bar_panels_png(
    title: str,
    labels: list[str],
    panels: list[tuple[str, list[float], str, str]],
) -> bytes:
    """Draw one column chart per ``(heading, values, colour, kind)`` panel.

    Each panel has its own y-axis; different measures never share a scale.
    """
    width, panel_height, top = _CHART_WIDTH, 340, 100
    image = Image.new("RGB", (width, top + panel_height * len(panels) + 12), "#ffffff")
    draw = ImageDraw.Draw(image)
    heading_font, axis_font = _chart_fonts()
    _draw_title(draw, title)
    for index, (heading, values, colour, kind) in enumerate(panels):
        y = top + index * panel_height
        draw.text((_CHART_MARGIN, y), heading, font=heading_font, fill=_TEXT_PRIMARY)
        chart_top, base = y + 96, y + panel_height - 66
        peak = max(values, default=0.0)
        ticks = _axis_ticks(peak, kind)
        left, slot, scale = _plot_frame(draw, axis_font, labels, ticks, kind, chart_top, base)
        bar_width = min(_BAR_MAX_WIDTH, slot * 0.72)
        for position, value in enumerate(values):
            if value <= 0:
                continue
            centre = left + (position + 0.5) * slot
            height = max(2.0, value * scale)
            _draw_column(
                draw, (centre - bar_width / 2, base - height, centre + bar_width / 2, base),
                colour, rounded=True,
            )
        if peak > 0:
            position = values.index(peak)
            _label_peak(
                draw, axis_font, _value_label(peak, kind),
                left + (position + 0.5) * slot, base - peak * scale, left,
            )
    return _png_bytes(image)


def _stacked_png(
    title: str,
    heading: str,
    labels: list[str],
    series: list[tuple[str, list[float], str]],
) -> bytes:
    """Draw stacked duration columns with a legend of ``(name, values, colour)`` series."""
    width = _CHART_WIDTH
    left_edge, right = _CHART_MARGIN, width - _CHART_MARGIN
    heading_font, axis_font = _chart_fonts()
    # Lay the legend out first so the image height fits it.
    legend: list[tuple[float, float, str, str]] = []
    x, y = float(left_edge), 100.0
    for name, _, colour in series:
        text = name
        while text and axis_font.getlength(text) > 320:
            text = text[:-2] + "…" if len(text) > 2 else ""
        item_width = 42 + axis_font.getlength(text) + 30
        if x + item_width > right and x > left_edge:
            x, y = float(left_edge), y + 46
        legend.append((x, y, text, colour))
        x += item_width
    heading_y = y + 64
    chart_top = heading_y + 96
    base = chart_top + 340
    image = Image.new("RGB", (width, int(base + 72)), "#ffffff")
    draw = ImageDraw.Draw(image)
    _draw_title(draw, title)
    for item_x, item_y, text, colour in legend:
        draw.rounded_rectangle((item_x, item_y + 4, item_x + 30, item_y + 34), radius=4, fill=colour)
        draw.text((item_x + 42, item_y + 2), text, font=axis_font, fill=_TEXT_PRIMARY)
    draw.text((left_edge, heading_y), heading, font=heading_font, fill=_TEXT_PRIMARY)
    totals = [sum(values[index] for _, values, _ in series) for index in range(len(labels))]
    peak = max(totals, default=0.0)
    ticks = _axis_ticks(peak, "duration")
    left, slot, scale = _plot_frame(draw, axis_font, labels, ticks, "duration", chart_top, base)
    bar_width = min(_BAR_MAX_WIDTH, slot * 0.72)
    for position in range(len(labels)):
        centre = left + (position + 0.5) * slot
        segments = [(values[position] * scale, colour) for _, values, colour in series if values[position] > 0]
        bottom = base
        for number, (height, colour) in enumerate(segments):
            top_edge = bottom - height
            is_top = number == len(segments) - 1
            # A surface-coloured gap separates touching segments.
            gap = 0 if is_top else min(_SEGMENT_GAP, height / 2)
            _draw_column(
                draw, (centre - bar_width / 2, top_edge + gap, centre + bar_width / 2, bottom),
                colour, rounded=is_top,
            )
            bottom = top_edge
    if peak > 0:
        position = totals.index(peak)
        _label_peak(
            draw, axis_font, _duration(peak), left + (position + 0.5) * slot, base - peak * scale, left,
        )
    return _png_bytes(image)


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _append_fitting(lines: list[str], entries: list[str]) -> int:
    """Append ``entries`` to ``lines`` while the text fits; return how many were left out."""
    for index, entry in enumerate(entries):
        if len("\n".join((*lines, entry))) > _LIST_LIMIT:
            return len(entries) - index
        lines.append(entry)
    return 0


def _is_active(entry: dict[str, Any]) -> bool:
    return int(entry["messages"]) > 0 or float(entry["voice_seconds"]) > 0


def _is_observed(entry: dict[str, Any]) -> bool:
    """True for a day with recorded activity or a finished, fully watched day."""
    return _is_active(entry) or bool(entry.get("watched"))


def _streaks(series: list[dict[str, Any]]) -> tuple[int, int]:
    """Return the longest and current runs of active days in a daily series.

    Today does not break the current run until it ends without activity.
    """
    longest = run = 0
    for entry in series:
        run = run + 1 if _is_active(entry) else 0
        longest = max(longest, run)
    days = list(series)
    if days and not _is_active(days[-1]):
        days.pop()
    current = 0
    for entry in reversed(days):
        if not _is_active(entry):
            break
        current += 1
    return longest, current


def _ghost_days(series: list[dict[str, Any]]) -> tuple[int, int, str | None, str | None]:
    """Return total ghost days and the longest ghost run with its first and last day.

    A ghost day is a finished day the collector watched in full with no
    messages and no voice time. Unwatched days are unknown and break a run.
    """
    total = longest = run = 0
    run_start: str | None = None
    best: tuple[str | None, str | None] = (None, None)
    for entry in series:
        if bool(entry.get("watched")) and not _is_active(entry):
            total += 1
            run += 1
            run_start = entry["day"] if run == 1 else run_start
            if run > longest:
                longest, best = run, (run_start, entry["day"])
        else:
            run = 0
    return total, longest, best[0], best[1]


def _short_day(day: str) -> str:
    return date.fromisoformat(day).strftime("%a %b %-d")


def _bucket_unit(day_count: int) -> str:
    if day_count <= 62:
        return "day"
    return "week" if day_count <= 62 * 7 else "month"


def _bucket_key(day: date, unit: str) -> date:
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    return day


def _bucket_label(key: date, unit: str, day_count: int) -> str:
    if unit == "month":
        return key.strftime("%b %y")
    if unit == "day" and day_count <= 14:
        return key.strftime("%a %-d")
    return key.strftime("%b %-d")


def _bucket_series(
    series: list[dict[str, Any]],
) -> tuple[str, list[str], list[float], list[float]]:
    """Group a daily series by day, week, or month so the chart stays readable."""
    unit = _bucket_unit(len(series))
    buckets: dict[date, list[float]] = {}
    for entry in series:
        totals = buckets.setdefault(_bucket_key(date.fromisoformat(entry["day"]), unit), [0.0, 0.0])
        totals[0] += float(entry["messages"])
        totals[1] += float(entry["voice_seconds"])
    keys = sorted(buckets)
    return (
        unit,
        [_bucket_label(key, unit, len(series)) for key in keys],
        [buckets[key][0] for key in keys],
        [buckets[key][1] for key in keys],
    )


def _retention_note(result: dict[str, Any], timezone: str, what: str) -> str | None:
    if float(result["since"]) <= float(result["period_start"]):
        return None
    since = _local_time(float(result["since"]), timezone, date_only=True)
    return f"{what} are kept only for recent activity, so this covers activity since {since}."


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_PREVIOUS_LABELS = {
    "today": "yesterday",
    "week": "last week",
    "last7": "the previous 7 days",
    "month": "last month",
}
_BURST_GAP_SECONDS = 120.0
_BURST_BINS = ((1, 1, "1"), (2, 2, "2"), (3, 4, "3–4"), (5, 9, "5–9"), (10, 19, "10–19"), (20, None, "20+"))
TREND_CHOICES = [
    app_commands.Choice(name="Day by day", value="daily"),
    app_commands.Choice(name="Compared with last period", value="compare"),
    app_commands.Choice(name="Time of day", value="hours"),
    app_commands.Choice(name="Day of week", value="weekdays"),
    app_commands.Choice(name="Company over time", value="company"),
    app_commands.Choice(name="Message bursts", value="bursts"),
]


def _daily_trend(series: list[dict[str, Any]], label: str, person: _Person) -> tuple[str, bytes]:
    unit, labels, messages, voice = _bucket_series(series)
    busiest = max(series, key=lambda entry: (int(entry["messages"]), float(entry["voice_seconds"])))
    active = sum(1 for entry in series if _is_active(entry))
    observed = sum(1 for entry in series if _is_observed(entry))
    longest, current = _streaks(series)
    ghosts, ghost_run, ghost_start, ghost_end = _ghost_days(series)
    lines = [
        f"**{person.safe}'s day-by-day trend — {label}**",
        f"Busiest day: **{_short_day(busiest['day'])}** — {int(busiest['messages']):,} messages, "
        f"{_duration(float(busiest['voice_seconds']))} in voice.",
        f"Active days: **{active}** of {observed} observed "
        f"(days with activity or watched in full).",
        f"Longest active streak: **{_plural(longest, 'day')}**; "
        f"current streak: **{_plural(current, 'day')}**.",
    ]
    if ghost_run:
        span = _short_day(ghost_start) if ghost_start == ghost_end else (
            f"{_short_day(ghost_start)} – {_short_day(ghost_end)}"
        )
        lines.append(
            f"Ghost days: **{ghosts}**; longest ghost streak **{_plural(ghost_run, 'day')}** ({span})."
        )
    lines.append(
        "A ghost day is a finished day the tracker watched in full with no messages or voice; "
        "days it was not watching don't count."
    )
    if unit != "day":
        lines.append(f"The chart groups days by {unit} so the bars stay readable.")
    png = _bar_panels_png(
        f"{person.name}'s activity per {unit} — {label}",
        labels,
        [
            (f"Messages per {unit}", messages, _PIE_COLORS[0], "count"),
            (f"Observed voice time per {unit}", voice, _PIE_COLORS[1], "duration"),
        ],
    )
    return "\n".join(lines), png


def _weekday_trend(series: list[dict[str, Any]], label: str, person: _Person) -> tuple[str, bytes]:
    occurrences = [0] * 7
    messages = [0.0] * 7
    voice = [0.0] * 7
    for entry in series:
        if not _is_observed(entry):
            continue
        weekday = date.fromisoformat(entry["day"]).weekday()
        occurrences[weekday] += 1
        messages[weekday] += float(entry["messages"])
        voice[weekday] += float(entry["voice_seconds"])
    avg_messages = [total / count if count else 0.0 for total, count in zip(messages, occurrences)]
    avg_voice = [total / count if count else 0.0 for total, count in zip(voice, occurrences)]
    chattiest = max(range(7), key=lambda day: (avg_messages[day], -day))
    loudest = max(range(7), key=lambda day: (avg_voice[day], -day))
    lines = [f"**{person.safe}'s week pattern — {label}**"]
    if avg_messages[chattiest] > 0:
        lines.append(
            f"Chattiest day: **{_WEEKDAY_NAMES[chattiest]}** — {avg_messages[chattiest]:.1f} messages on average."
        )
    if avg_voice[loudest] > 0:
        lines.append(
            f"Most voice time: **{_WEEKDAY_NAMES[loudest]}** — {_duration(avg_voice[loudest])} on average."
        )
    lines.append(
        "Averages count days with activity or a full day of watching; "
        "quiet days the tracker didn't fully watch are left out."
    )
    if sum(occurrences) < 14:
        lines.append("Each weekday appears at most twice here; a longer period gives a fairer pattern.")
    png = _bar_panels_png(
        f"{person.name}'s average day of the week — {label}",
        list(_WEEKDAYS),
        [
            ("Average messages per day", avg_messages, _PIE_COLORS[0], "average"),
            ("Average observed voice time per day", avg_voice, _PIE_COLORS[1], "duration"),
        ],
    )
    return "\n".join(lines), png


def _hour_trend(
    messages: dict[str, Any], voice: dict[str, Any], label: str, timezone: str, person: _Person
) -> tuple[str, bytes | None]:
    zone = get_timezone(timezone)
    message_hours = [0.0] * 24
    for created_at in messages["times"]:
        message_hours[datetime.fromtimestamp(float(created_at), tz=zone).hour] += 1
    voice_hours = [float(value) for value in voice["hours"]]
    message_total, voice_total = sum(message_hours), sum(voice_hours)
    if message_total == 0 and voice_total == 0:
        return f"No messages or voice time with retained times have been recorded for {label}.", None
    lines = [f"**{person.safe}'s clock — {label}**"]
    if message_total:
        peak = max(range(24), key=lambda hour: (message_hours[hour], -hour))
        lines.append(
            f"Peak message hour: **{peak:02d}:00–{(peak + 1) % 24:02d}:00** "
            f"with {int(message_hours[peak]):,} of {int(message_total):,} messages."
        )
    if voice_total:
        peak = max(range(24), key=lambda hour: (voice_hours[hour], -hour))
        lines.append(
            f"Peak voice hour: **{peak:02d}:00–{(peak + 1) % 24:02d}:00** "
            f"with {_duration(voice_hours[peak])} of {_duration(voice_total)}."
        )
    shares = []
    if message_total:
        shares.append(f"**{sum(message_hours[0:5]) / message_total:.0%}** of messages")
    if voice_total:
        shares.append(f"**{sum(voice_hours[0:5]) / voice_total:.0%}** of voice time")
    lines.append(f"Night owl share (00:00–05:00): {' and '.join(shares)}.")
    lines.append(f"Hours use the {timezone} timezone.")
    note = _retention_note(messages, timezone, "Send times and voice sessions")
    if note:
        lines.append(note)
    png = _bar_panels_png(
        f"{person.name} by hour of day — {label}",
        [f"{hour:02d}" for hour in range(24)],
        [
            ("Messages by hour", message_hours, _PIE_COLORS[0], "count"),
            ("Observed voice time by hour", voice_hours, _PIE_COLORS[1], "duration"),
        ],
    )
    return "\n".join(lines), png


def _change(current: float, previous: float) -> str:
    if previous <= 0:
        return "no change" if current <= 0 else "new this period"
    ratio = current / previous - 1
    if abs(ratio) < 0.005:
        return "no change"
    return f"{'up' if ratio > 0 else 'down'} {abs(ratio):.0%}"


def _compare_trend(
    result: dict[str, Any], period: str, label: str, person: _Person
) -> tuple[str, bytes | None]:
    previous = result["previous"]
    if previous is None:
        reason = result.get("reason")
        if reason == "all":
            return "All time has no previous period. Pick today, the last 7 days, this week, or this month to compare.", None
        if reason == "untracked":
            return (
                f"Tracking began partway through {_PREVIOUS_LABELS[period]}, so there's no fair "
                "comparison yet.",
                None,
            )
        return (
            f"{_PREVIOUS_LABELS[period].capitalize()} is older than the retained detail, "
            "so it can't be compared to the minute.",
            None,
        )
    current = result["current"]
    previous_label = _PREVIOUS_LABELS[period]
    lines = [
        f"**{person.safe} vs {previous_label} — {label} so far**",
        f"Compared with {previous_label} up to the same point.",
        f"Messages: **{current['messages']:,}** vs {previous['messages']:,} "
        f"({_change(current['messages'], previous['messages'])}).",
        f"Observed voice time: **{_duration(current['voice_seconds'])}** vs "
        f"{_duration(previous['voice_seconds'])} "
        f"({_change(current['voice_seconds'], previous['voice_seconds'])}).",
        f"Voice visits: **{current['voice_visits']:,}** vs {previous['voice_visits']:,} "
        f"({_change(current['voice_visits'], previous['voice_visits'])}).",
    ]
    watched = []
    for window in (previous, current):
        length = float(window["end"]) - float(window["start"])
        watched.append(1.0 if length <= 0 else min(1.0, float(window["watched_seconds"]) / length))
    if min(watched) < 0.99:
        possessive = f"{previous_label}'" if previous_label.endswith("s") else f"{previous_label}'s"
        lines.append(
            f"The tracker watched {watched[0]:.0%} of {possessive} window and "
            f"{watched[1]:.0%} of this one; unwatched time is not filled in."
        )
    names = [previous_label.capitalize(), f"{label.capitalize()}"]
    png = _bar_panels_png(
        f"{person.name} vs {previous_label} — same point in time",
        names,
        [
            ("Messages", [float(previous["messages"]), float(current["messages"])], _PIE_COLORS[0], "count"),
            (
                "Observed voice time",
                [float(previous["voice_seconds"]), float(current["voice_seconds"])],
                _PIE_COLORS[1],
                "duration",
            ),
        ],
    )
    return "\n".join(lines), png


def _bursts(times: list[float]) -> list[tuple[float, float, int]]:
    """Group sorted send times into ``(start, end, count)`` runs split by quiet gaps."""
    bursts: list[tuple[float, float, int]] = []
    for created_at in times:
        if bursts and created_at - bursts[-1][1] <= _BURST_GAP_SECONDS:
            start, _, count = bursts[-1]
            bursts[-1] = (start, created_at, count + 1)
        else:
            bursts.append((created_at, created_at, 1))
    return bursts


def _burst_trend(
    result: dict[str, Any], label: str, timezone: str, person: _Person
) -> tuple[str, bytes | None]:
    bursts = _bursts(sorted(float(value) for value in result["times"]))
    if not bursts:
        return f"No messages with retained send times have been recorded for {label}.", None
    total = sum(count for _, _, count in bursts)
    biggest = max(bursts, key=lambda burst: (burst[2], -burst[0]))
    rapid = sum(count for _, _, count in bursts if count >= 5)
    lines = [
        f"**{person.safe}'s message bursts — {label}**",
        f"Biggest burst: **{biggest[2]:,} messages** in {_duration(biggest[1] - biggest[0])} "
        f"({_local_time(biggest[0], timezone)}).",
        f"Average burst: **{total / len(bursts):.1f} messages** across {len(bursts):,} bursts.",
        f"Rapid fire: **{rapid / total:.0%}** of messages came in bursts of 5 or more.",
        "A burst is messages sent within 2 minutes of the previous one, across tracked channels.",
    ]
    note = _retention_note(result, timezone, "Send times")
    if note:
        lines.append(note)
    counts = [
        float(sum(1 for _, _, count in bursts if count >= low and (high is None or count <= high)))
        for low, high, _ in _BURST_BINS
    ]
    png = _bar_panels_png(
        f"{person.name}'s burst sizes — {label}",
        [name for _, _, name in _BURST_BINS],
        [("Bursts, by messages in each burst", counts, _PIE_COLORS[0], "count")],
    )
    return "\n".join(lines), png


async def _company_trend(
    bot: Any, interaction: discord.Interaction, person: _Person, period: str, label: str,
    count: str = "split",
) -> tuple[str, bytes | None]:
    now = time.time()
    rows = await bot.store.company_daily(
        person.user_id, period, now, include_live=_collection_reliable(bot)
    )
    rows = _visible_company_rows(bot, interaction, rows)
    if not rows:
        return (
            f"No companion time has been observed for {person.safe} {label} in voice channels visible to this report.",
            None,
        )
    # Buckets run from the first day with company data, which started with that feature.
    first = min(date.fromisoformat(row["day"]) for row in rows)
    last = max(local_date(now, bot.config.timezone), max(date.fromisoformat(row["day"]) for row in rows))
    day_count = (last - first).days + 1
    unit = _bucket_unit(day_count)
    keys = sorted({_bucket_key(first + timedelta(days=offset), unit) for offset in range(day_count)})
    position = {key: index for index, key in enumerate(keys)}
    by_member: dict[int, list[float]] = {}
    for row in rows:
        values = by_member.setdefault(int(row["member_id"]), [0.0] * len(keys))
        values[position[_bucket_key(date.fromisoformat(row["day"]), unit)]] += float(
            row.get(_company_field(count), 0.0)
        )
    peers = sorted(
        (member_id for member_id in by_member if member_id != 0),
        key=lambda member_id: (-sum(by_member[member_id]), member_id),
    )
    shown = peers[:5]
    stacks: list[tuple[int, list[float]]] = [(member_id, by_member[member_id]) for member_id in shown]
    if 0 in by_member:
        stacks.append((0, by_member[0]))
    if len(peers) > 5:
        stacks.append((-1, [sum(by_member[member_id][i] for member_id in peers[5:]) for i in range(len(keys))]))
    recent = list(enumerate(keys))[-6:]
    winners: dict[int, int | None] = {}
    for index, _ in recent:
        ranked = [member_id for member_id in peers if by_member[member_id][index] > 0]
        winners[index] = max(
            ranked, key=lambda member_id: (by_member[member_id][index], -member_id), default=None
        )
    named = list(dict.fromkeys(
        [member_id for member_id, _ in stacks]
        + [member_id for member_id in winners.values() if member_id is not None]
    ))
    guild = _guild(bot)
    names = await asyncio.gather(*(_company_name(bot, guild, member_id) for member_id in named))
    name_of = dict(zip(named, names))
    full = count == "full"
    title = f"**{person.safe}'s company over time — {label}**"
    lines = [f"{title} (full time with each person)" if full else title]
    lines.append(f"Top companion each {unit} (most recent last):")
    for index, key in recent:
        best = winners[index]
        when = _bucket_label(key, unit, day_count)
        if best is None:
            alone = by_member.get(0, [0.0] * len(keys))[index]
            lines.append(f"{when}: {'alone — ' + _duration(alone) if alone > 0 else 'no company time'}")
        else:
            lines.append(f"{when}: **{_safe_name(name_of[best])}** — {_duration(by_member[best][index])}")
    if full:
        lines.append(
            "Each person is credited with every minute they shared, so stacked bars can add up to more "
            "than the observed time; only observed time since companion tracking began is included."
        )
    else:
        lines.append(
            "Each shared minute is split evenly among the people present; only observed time since "
            "companion tracking began is included."
        )
    png = await _render(
        _stacked_png,
        f"{person.name}'s company per {unit} — {label}",
        f"Full shared time per {unit}, by companion (overlapping)" if full
        else f"Observed voice time per {unit}, by companion",
        [_bucket_label(key, unit, day_count) for key in keys],
        [
            (name_of[member_id], values, _PIE_COLORS[index])
            for index, (member_id, values) in enumerate(stacks)
        ],
    )
    return "\n".join(lines), png


async def _trend_report(
    bot: Any, interaction: discord.Interaction, person: _Person, period: str, kind: str,
    count: str = "split",
) -> tuple[str, bytes | None]:
    now = time.time()
    label = _period_label(period)
    timezone = bot.config.timezone
    reliable = _collection_reliable(bot)
    user_id = person.user_id
    # Data is read here; each builder then writes its text and draws its chart
    # on a worker thread.
    if kind == "hours":
        messages = await bot.store.message_times(user_id, period, now)
        voice = await bot.store.voice_hours(user_id, period, now, include_live=reliable)
        return await _render(_hour_trend, messages, voice, label, timezone, person)
    if kind == "bursts":
        result = await bot.store.message_times(user_id, period, now)
        return await _render(_burst_trend, result, label, timezone, person)
    if kind == "compare":
        result = await bot.store.period_comparison(user_id, period, now, include_live=reliable)
        return await _render(_compare_trend, result, period, label, person)
    if kind == "company":
        return await _company_trend(bot, interaction, person, period, label, count)
    series = await bot.store.daily_trend(user_id, period, now, include_live=reliable)
    if not any(_is_active(entry) for entry in series):
        return f"No activity has been recorded for {label}, so there is no trend to chart.", None
    if kind == "weekdays":
        return await _render(_weekday_trend, series, label, person)
    return await _render(_daily_trend, series, label, person)


async def _online_text(bot: Any, person: _Person) -> str:
    """Request a person's current guild presence without keeping a history."""
    guild = _guild(bot)
    if guild is None or getattr(guild, "unavailable", False):
        return f"I can't check {person.safe}'s Discord status while the server is unavailable."
    try:
        members = await asyncio.wait_for(
            guild.query_members(user_ids=[person.user_id], presences=True, cache=False),
            timeout=10,
        )
    except (TimeoutError, discord.ClientException):
        return f"I couldn't check {person.safe}'s Discord status right now. Try again shortly."
    member = next((item for item in members if item.id == person.user_id), None)
    if member is None:
        return f"I couldn't find {person.safe} in this server to check their Discord status."
    status = member.status
    if status == discord.Status.online:
        return f"{person.safe} is online right now."
    if status == discord.Status.idle:
        return f"{person.safe} is online (away) right now."
    if status == discord.Status.dnd:
        return f"{person.safe} is online (Do Not Disturb) right now."
    if status in (discord.Status.offline, discord.Status.invisible):
        return f"{person.safe} appears offline or invisible right now."
    return f"I couldn't determine {person.safe}'s Discord status right now."


async def _records_text(bot: Any, interaction: discord.Interaction, person: _Person) -> str:
    now = time.time()
    records = await bot.store.records(person.user_id, now, include_live=_collection_reliable(bot))
    state = await bot.store.state()
    timezone = bot.config.timezone
    # A person's clock starts when both the database and their tracking began.
    started = _local_time(
        max(float(state["tracking_since"]), person.since), timezone, date_only=True
    )
    lines = [f"**{person.safe}'s personal records**", f"Measurement period: since {started} ({timezone})."]

    busiest_day = records.get("busiest_day")
    busiest_messages = int(records.get("busiest_day_messages", 0) or 0)
    longest_seconds = float(records.get("longest_visit_seconds", 0) or 0)
    longest_at = records.get("longest_visit_at")
    current_seconds = records.get("current_visit_seconds")
    has_record = False
    if busiest_day is not None and busiest_messages > 0:
        day = date.fromisoformat(busiest_day).strftime("%a %b %-d, %Y")
        lines.append(f"Most messages in one day: **{busiest_messages:,}** on {day}.")
        has_record = True
    if longest_seconds > 0 and longest_at is not None:
        start = _local_time(longest_at, timezone)
        lines.append(
            f"Longest fully observed voice visit: **{_duration(longest_seconds)}**, "
            f"started {start}."
        )
        has_record = True
    peers = [
        (member_id, seconds)
        for member_id, seconds in (await _company_seconds(bot, interaction, person, "all")).items()
        if member_id > 0
    ]
    if peers:
        member_id, seconds = min(peers, key=lambda item: (-item[1], item[0]))
        name = _safe_name(await _company_name(bot, _guild(bot), member_id))
        lines.append(
            f"Top voice companion: **{name}** — {_duration(seconds)} of shared voice time "
            "since companion tracking began (split evenly when more people were present)."
        )
        has_record = True
    if not has_record:
        lines.append("No personal records have been recorded yet.")
    if current_seconds is not None and float(current_seconds) > 0:
        note = (
            "it can set the record once it ends"
            if records.get("current_visit_complete_start")
            else "its start was not observed, so it cannot set the record"
        )
        lines.append(
            f"Current voice visit so far: **{_duration(float(current_seconds))}** ({note})."
        )
    return "\n".join(lines)


def _top_metric(metric: str) -> tuple[str, str]:
    """Return the ranking field and label for a ``top`` metric choice."""
    key = str(metric).strip().lower().replace(" ", "_")
    if key == "voice":
        return "voice_seconds", "observed voice time"
    if key == "active_days":
        return "active_days", "active days"
    return "messages", "messages"


def _top_value(field: str, value: float) -> str:
    if field == "voice_seconds":
        return _duration(value)
    if field == "active_days":
        return _plural(int(value), "day")
    return _plural(int(value), "message")


async def _top_text(bot: Any, period: str, metric: str) -> str:
    """Rank tracked people (and formerly tracked people with activity) by one metric."""
    field, metric_label = _top_metric(metric)
    label = _period_label(period)
    rows = await bot.store.ranking(period, time.time(), include_live=_collection_reliable(bot))
    ranked = sorted(
        (row for row in rows if float(row[field]) > 0),
        key=lambda row: (-float(row[field]), int(row["user_id"])),
    )
    if not ranked:
        return f"No {metric_label} to rank for {label}."
    top = ranked[:_LEADERBOARD_SIZE]
    guild = _guild(bot)
    names = await asyncio.gather(*(_company_name(bot, guild, int(row["user_id"])) for row in top))
    lines = [f"**Top {metric_label} — {label}**"]
    for index, (name, row) in enumerate(zip(names, top)):
        formerly = "" if row.get("tracked", True) else " (no longer tracked)"
        lines.append(
            f"{_rank_label(index)} **{_safe_name(name)}** — "
            f"{_top_value(field, float(row[field]))}{formerly}"
        )
    if len(ranked) > len(top):
        lines.append(f"…and {len(ranked) - len(top):,} more.")
    lines.append("Only activity observed while each person was tracked is counted.")
    return "\n".join(lines)


async def _tracked_list_text(bot: Any) -> str:
    """List who is tracked now, then former people whose history is still kept."""
    now = time.time()
    rows = await bot.store.tracked_users()
    timezone = bot.config.timezone
    # A former person still "has history" when the all-time ranking shows activity.
    with_history = {
        int(row["user_id"])
        for row in await bot.store.ranking("all", now, include_live=False)
        if (
            int(row["messages"]) > 0 or float(row["voice_seconds"]) > 0
            or int(row["voice_visits"]) > 0 or int(row["active_days"]) > 0
        )
    }
    active = [row for row in rows if row["active"]]
    former = [row for row in rows if not row["active"] and int(row["user_id"]) in with_history]
    guild = _guild(bot)
    shown = [*active, *former]
    labels = await asyncio.gather(*(
        _admin_label(bot, guild, int(row["user_id"])) for row in shown
    ))
    active_entries: list[str] = []
    former_entries: list[str] = []
    for row, name in zip(shown, labels):
        date_text = _local_time(row["updated_at"], timezone, date_only=True)
        if row["active"]:
            active_entries.append(f"{name} (since {date_text})")
        else:
            former_entries.append(f"{name} (stopped {date_text})")
    lines = [f"**Tracked people ({len(active):,})**"]
    if not active:
        lines.append("Nobody is tracked yet. An admin can add people with `/flock track add`.")
    omitted = _append_fitting(lines, active_entries)
    if omitted:
        omitted += len(former_entries)
    elif former_entries:
        lines.append("Formerly tracked (history kept):")
        omitted = _append_fitting(lines, former_entries)
    if omitted:
        lines.append(f"…and {omitted:,} more people")
    return "\n".join(lines)


def _introduction_text(bot: Any) -> str:
    parts = [
        "Hi, I'm **Flock CCTV**, the security camera bolted to the corner of this server.",
        "I keep an eye on a short list of people, counting their messages and timing how long they hang around in voice "
        "(I never keep what anyone actually says), and I turn it all into stats, charts, leaderboards, and the occasional roast.",
    ]
    if _leland_id(bot) is not None:
        parts.append("Also, I hate Leland. Nothing personal, it's in my firmware.")
    parts.append("Type `/flock help` to see everything I can do.")
    return " ".join(parts)


def _help_text(bot: Any) -> str:
    timezone = bot.config.timezone
    lines = [
        "**Flock commands**",
        "Flock tracks an admin-managed list of people. Per-person reports take an optional `user` (default: you).",
        "`/flock stats` — messages, observed voice time, active days, visits, and coverage gaps (week by default).",
        "`/flock records` — busiest day, longest observed voice visit, and top companion.",
        "`/flock where` — last observed voice channel and time.",
        "`/flock company` — pie chart of who shared observed voice time; `count:full` credits whole group calls.",
        "`/flock leaderboard` — who spent the most voice time with someone, counting whole group calls (all time by default).",
        "`/flock trends` — day by day, versus last period, time of day, day of week, company, or message bursts (last 7 days by default).",
        "`/flock online` — Discord status of a tracked person; away counts as online.",
        "`/flock roast` — a light joke from a recorded statistic.",
        "`/flock top` — ranks everyone tracked by messages, voice time, or active days (this week by default).",
        "`/flock track list` — who is tracked now, plus former people whose history is kept.",
        "`/flock track add` and `remove` — tracker admins choose who is tracked; untracking keeps history.",
        "`/flock help`, `about`, and `version` — this guide, status, and release; `/flock introduce` says hi publicly.",
        "`/flock update`, `/flock pause`, and `/flock resume` — tracker admins check for updates or pause and resume collection.",
        "`/flock debug` — admin health, uptime, errors, people, alerts.",
        "`/flock delete-data` — tracker admins erase one person's statistics (with `user`) or everyone's, after confirmation.",
    ]
    if _leland_id(bot) is not None:
        lines.append(
            "`/flock evil-mode` and `/flock reaction-mode` — tracker admins toggle Leland's upside-down reposts and 😂/👸 reactions."
        )
    lines.extend(
        [
            "`/flock admin add`, `remove`, and `list` — the owner manages tracker admins privately.",
            f"Reports use the **{timezone}** timezone. Message counts use creation events; voice time is observed connection time, not speaking time.",
            "Collection is limited to the configured server and channels. No message text is stored.",
        ]
    )
    return "\n".join(lines)


async def _status_text(bot: Any) -> str:
    state = await bot.store.state()
    timezone = bot.config.timezone
    tracker = bot.tracker
    paused = bool(state.get("paused", False))
    last_checkpoint = state.get("last_checkpoint")
    health = "connected" if getattr(tracker, "connected", False) else "disconnected"
    if not getattr(tracker, "guild_is_available", False):
        health += "; configured server unavailable"
    if paused:
        collection_state = "paused"
    elif not _collection_reliable(bot):
        collection_state = "unavailable"
    else:
        collection_state = "running"
    collector_state = "paused" if paused else ("enabled" if collection_state == "running" else "unavailable")
    lines = [
        "**About Flock**",
        f"Version: **{version_string()}**.",
        f"Gateway: **{health}**.",
        f"Collection: **{collection_state}**.",
    ]
    try:
        tracked = sum(1 for row in await bot.store.tracked_users() if row["active"])
        lines.append(f"Tracked people: **{tracked:,}**.")
    except Exception:
        logger.exception("Could not read the tracked people count")
        lines.append("The tracked people count is temporarily unavailable; the error was logged.")
    if _leland_id(bot) is not None:
        lines.extend(
            [
                f"Evil Leland mode: **{'on' if state.get('evil_mode', False) else 'off'}**.",
                f"Reaction mode: **{'on' if state.get('reaction_mode', False) else 'off'}**.",
            ]
        )
    lines.append(
        f"Message collector: **{collector_state}**; voice collector: **{collector_state}**."
    )
    lines.append(f"Last checkpoint: {_local_time(last_checkpoint, timezone)}.")
    try:
        gaps = float(await bot.store.coverage_gap_seconds(time.time()) or 0)
        lines.append(f"Recorded coverage gaps: **{_duration(gaps)}**.")
    except Exception:
        logger.exception("Could not read status coverage totals")
        lines.append("Coverage totals are temporarily unavailable; the error was logged.")
    database_path = getattr(bot.config, "database_path", None)
    if database_path is not None:
        status = await asyncio.to_thread(update_status.read_status, database_path)
        lines.append(
            discord.utils.escape_mentions(update_status.status_line(status, timezone))
        )
    if getattr(tracker, "last_error", None):
        lines.append("Last tracker error: **recorded**; admins can see it with `/flock debug errors`.")
    if paused and state.get("paused_by") is not None:
        lines.append("Pause remains in effect across restarts.")
    return "\n".join(lines)


_GAP_REASONS = {
    "disconnect": "Discord connection lost",
    "process_restart": "bot stopped or restarted",
}
_UPTIME_COLORS = (_PIE_COLORS[2], _PIE_COLORS[7], _BASELINE)
_UPTIME_OUTAGES_SHOWN = 8
_ERRORS_SHOWN = 15


def _gap_reason(reason: str) -> str:
    return _GAP_REASONS.get(reason, discord.utils.escape_markdown(reason))


def _ago(seconds: float) -> str:
    return f"{_duration(seconds)} ago"


def _size(value: float | None) -> str:
    if value is None:
        return "unknown"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def _percent(part: float, whole: float) -> str:
    return "n/a" if whole <= 0 else f"{100 * part / whole:.1f}%"


def _gap_span(gap: dict[str, Any], timezone: str) -> str:
    start = _local_time(gap["started_at"], timezone)
    if gap.get("ended_at") is None:
        return f"{start} – still open"
    zone = get_timezone(timezone)
    began = datetime.fromtimestamp(float(gap["started_at"]), tz=zone)
    ended = datetime.fromtimestamp(float(gap["ended_at"]), tz=zone)
    if began.date() == ended.date():
        return f"{began.strftime('%b %-d, %Y %H:%M')} – {ended.strftime('%H:%M %Z')}"
    return f"{start} – {_local_time(gap['ended_at'], timezone)}"


def outage_alert_text(outages: list[dict[str, Any]], timezone: str) -> str:
    """The owner's DM about outages that ended since the last alert."""
    if len(outages) == 1:
        gap = outages[0]
        lines = [
            f"**Flock was not watching for {_duration(gap['seconds'])}** "
            f"({_gap_reason(gap['reason'])}), {_gap_span(gap, timezone)}."
        ]
    else:
        lines = [f"**Flock had {len(outages)} outages:**"]
        lines.extend(
            f"• {_duration(gap['seconds'])} ({_gap_reason(gap['reason'])}), {_gap_span(gap, timezone)}"
            for gap in outages[-10:]
        )
    lines.append(
        "That time counts as missing coverage, not quiet time. "
        "`/flock debug uptime` has the details; `/flock debug alerts` changes the threshold."
    )
    return "\n".join(lines)


async def _health_text(bot: Any) -> str:
    now = time.time()
    timezone = bot.config.timezone
    tracker = bot.tracker
    state = await bot.store.state()
    lines = ["**Flock health**"]
    started = getattr(bot, "started_at", None)
    if started is not None:
        lines.append(
            f"Bot process: up **{_duration(now - started)}** (since {_local_time(started, timezone)})."
        )
    since = getattr(tracker, "collection_since", None)
    problem = _collection_problem(bot)
    if state.get("paused", False):
        lines.append("Collection: **paused** by an admin.")
    elif problem is None and since is not None:
        lines.append(
            f"Collection: **running** for {_duration(now - since)} "
            f"(since {_local_time(since, timezone)})."
        )
    else:
        reason = {
            "disconnected": "the Discord connection is down",
            "guild_unavailable": "the configured server is unavailable",
        }.get(problem, "collection is still starting")
        lines.append(f"Collection: **not running**; {reason}.")
    checkpoint = state.get("last_checkpoint")
    if checkpoint is None:
        lines.append("Last checkpoint: not recorded yet.")
    else:
        age = max(0.0, now - float(checkpoint))
        line = f"Last checkpoint: {_local_time(checkpoint, timezone)} ({_ago(age)})."
        expected = 3 * float(getattr(bot.config, "checkpoint_seconds", 60))
        if problem is None and not state.get("paused", False) and age > expected:
            line += " ⚠️ That is older than expected; checkpoints may be failing."
        lines.append(line)
    error = getattr(tracker, "last_error", None)
    lines.append(
        f"Current error: **{discord.utils.escape_markdown(error)}**." if error else "Current error: none."
    )
    if hasattr(bot, "flush_error_log"):
        await bot.flush_error_log()
    errors = await bot.store.error_log(now, limit=0)
    lines.append(
        f"Logged problems: **{errors['day']:,}** in the last day, {errors['week']:,} in the last week "
        "(`/flock debug errors` lists them)."
    )
    files = await bot.store.files_summary()
    database = f"Database: **{_size(files['database_bytes'])}**"
    if files["wal_bytes"]:
        database += f" (+{_size(files['wal_bytes'])} write-ahead log)"
    if files["disk_free"] is not None:
        database += f"; disk free **{_size(files['disk_free'])}** of {_size(files['disk_total'])}"
        if files["disk_total"] and files["disk_free"] / files["disk_total"] < 0.1:
            database += " ⚠️ less than 10% left"
    lines.append(database + ".")
    daily = [item for item in files["backups"] if item["daily"]]
    if daily:
        newest = daily[0]["modified"]
        line = (
            f"Backups: **{len(daily)}** daily, newest {_local_time(newest, timezone)} ({_ago(now - newest)})."
        )
        if now - newest > 2 * 86_400:
            line += " ⚠️ No backup in over two days."
        lines.append(line)
    else:
        lines.append("Backups: **none yet**; the first one is written by daily maintenance.")
    pruned = files["pruned_before"]
    retention = f"Retention: message and visit detail kept for **{files['retention_days']} days**"
    if pruned is not None and pruned > files["tracking_since"]:
        retention += f" (detail before {_local_time(pruned, timezone, date_only=True)} pruned)"
    lines.append(retention + ".")
    minutes = await bot.store.outage_alert_minutes()
    lines.append(
        f"Outage alerts: the owner gets a DM after outages of **{_duration(minutes * 60)}** or longer."
        if minutes else "Outage alerts: **off**."
    )
    database_path = getattr(bot.config, "database_path", None)
    if database_path is not None:
        status = await asyncio.to_thread(update_status.read_status, database_path)
        lines.append(discord.utils.escape_mentions(update_status.status_line(status, timezone)))
    return "\n".join(lines)


async def _uptime_report(bot: Any, period: str) -> tuple[str, bytes | None]:
    timezone = bot.config.timezone
    label = _period_label(period)
    result = await bot.store.uptime(period, time.time(), include_live=_collection_reliable(bot))
    elapsed = float(result["end"]) - float(result["start"])
    lines = [f"**Flock uptime — {label}**"]
    if elapsed <= 0 or not result["days"]:
        lines.append("Nothing to report yet for this period.")
        return "\n".join(lines), None
    observed, outage, idle = float(result["observed"]), float(result["outage"]), float(result["idle"])
    lines.append(
        f"Watching: **{_percent(observed, elapsed)}** of the time "
        f"({_duration(observed)} of {_duration(elapsed)}, since {_local_time(result['start'], timezone)})."
    )
    outages = result["outages"]
    if outages:
        longest = max(outages, key=lambda gap: gap["seconds"])
        lines.append(
            f"Outages: **{len(outages)}**, totalling {_duration(outage)}; longest "
            f"{_duration(longest['seconds'])} ({_gap_reason(longest['reason'])})."
        )
    else:
        lines.append("Outages: **none**.")
    if idle > 0:
        lines.append(f"Paused or not collecting: {_duration(idle)}.")
    if outages:
        lines.append("Most recent outages:")
        for gap in reversed(outages[-_UPTIME_OUTAGES_SHOWN:]):
            lines.append(
                f"• {_gap_span(gap, timezone)} — {_duration(gap['seconds'])}, {_gap_reason(gap['reason'])}"
            )
        if len(outages) > _UPTIME_OUTAGES_SHOWN:
            lines.append(f"…and {len(outages) - _UPTIME_OUTAGES_SHOWN:,} earlier")
    lines.append("Outages count as missing coverage in every report, never as quiet time.")
    days = result["days"]
    unit = _bucket_unit(len(days))
    buckets: dict[date, list[float]] = {}
    for entry in days:
        totals = buckets.setdefault(_bucket_key(date.fromisoformat(entry["day"]), unit), [0.0, 0.0, 0.0])
        totals[0] += float(entry["observed"])
        totals[1] += float(entry["outage"])
        totals[2] += float(entry["idle"])
    keys = sorted(buckets)
    png = await _render(
        _stacked_png,
        f"Flock uptime — {label}",
        f"Time per {unit}",
        [_bucket_label(key, unit, len(days)) for key in keys],
        [
            (name, [buckets[key][index] for key in keys], colour)
            for index, (name, colour) in enumerate(
                zip(("Watching", "Outage", "Paused"), _UPTIME_COLORS)
            )
        ],
    )
    return "\n".join(lines), png


async def _errors_text(bot: Any) -> str:
    timezone = bot.config.timezone
    if hasattr(bot, "flush_error_log"):
        await bot.flush_error_log()
    result = await bot.store.error_log(time.time(), limit=_ERRORS_SHOWN)
    lines = ["**Flock error log**"]
    error = getattr(bot.tracker, "last_error", None)
    if error:
        lines.append(f"Current error: **{discord.utils.escape_markdown(error)}**.")
    if not result["recent"]:
        lines.append("No warnings or errors have been logged. 🎉")
        return "\n".join(lines)
    lines.append(
        f"**{result['day']:,}** in the last day, {result['week']:,} in the last week, "
        f"{result['total']:,} kept in total. Newest first:"
    )
    entries = [
        f"`{_local_time(entry['at'], timezone)}` {entry['level'].lower()} in "
        f"{discord.utils.escape_markdown(entry['source'])}: {_safe_name(entry['summary'])}"
        for entry in result["recent"]
    ]
    if _append_fitting(lines, entries):
        lines.append("…older entries left out to fit Discord's limit")
    lines.append("Only log lines and error types are kept; the service journal has full tracebacks.")
    return "\n".join(lines)


async def _person_debug_text(bot: Any, interaction: discord.Interaction, user: Any) -> str:
    now = time.time()
    timezone = bot.config.timezone
    user_id = int(user.id)
    name = _safe_name(_person_name(user, user_id))
    result = await bot.store.person_debug(user_id, now, include_live=_collection_reliable(bot))
    lines = [f"**Debug: {name}** ({user_id})"]
    if not result["known"]:
        lines.append(f"{name} has never been tracked, so nothing is stored about them.")
        return "\n".join(lines)
    guild = getattr(interaction, "guild", None) or _guild(bot)
    first = _local_time(result["tracking_since"], timezone)
    if result["active"]:
        line = f"Tracked: **yes**, first tracked {first}"
        if result["added_by"] is not None:
            line += f"; last added by {await _admin_label(bot, guild, result['added_by'])}"
        lines.append(line + ".")
    else:
        stopped = result["updated_at"]
        lines.append(
            f"Tracked: **no longer**, first tracked {first}"
            + (f", removed {_local_time(stopped, timezone)}." if stopped is not None else ".")
        )
    intervals = result["intervals"]
    if len(intervals) > 1:
        shown = intervals[-5:]
        spans = "; ".join(
            f"{_local_time(start, timezone)} – {'now' if stop is None else _local_time(stop, timezone)}"
            for start, stop in shown
        )
        more = f" (latest {len(shown)})" if len(intervals) > len(shown) else ""
        lines.append(f"Tracked-list stretches: **{len(intervals)}**{more}: {spans}.")
    elapsed = float(result["elapsed_seconds"])
    watched = float(result["watched_seconds"])
    lines.append(
        f"Watched: **{_duration(watched)}** of {_duration(elapsed)} since first tracked "
        f"(**{_percent(watched, elapsed)}**)."
    )
    missing = [
        (result["outage_seconds"], "outages"),
        (result["idle_seconds"], "paused or not collecting"),
        (result["untracked_seconds"], "off the tracked list"),
    ]
    parts = [f"{_duration(seconds)} {label}" for seconds, label in missing if seconds > 0]
    if parts:
        lines.append("Missing coverage: " + ", ".join(parts) + ".")
    outages = result["outages"]
    if outages:
        longest = max(outages, key=lambda gap: gap["seconds"])
        lines.append(
            f"Outages while tracked: **{len(outages)}**, longest {_duration(longest['seconds'])} "
            f"({_gap_reason(longest['reason'])}, {_local_time(longest['started_at'], timezone)})."
        )
    lines.append(
        f"Stored: {_plural(result['messages'], 'message record')}, "
        f"{_plural(result['voice_visits'], 'voice visit')} "
        f"({result['visits_complete']:,} watched start to end, "
        f"{result['voice_visits'] - result['visits_seen_start']:,} already in progress when seen), "
        f"{_plural(result['voice_segments'], 'voice segment')}, "
        f"{_plural(result['daily_rows'], 'daily total')} ({result['active_days']:,} active), "
        f"{_plural(result['records'], 'personal record')}."
    )
    if result["first_message_at"] is not None:
        lines.append(f"Oldest kept message record: {_local_time(result['first_message_at'], timezone)}.")
    if result["detail_since"] > result["tracking_since"]:
        lines.append(
            f"Detail before {_local_time(result['detail_since'], timezone, date_only=True)} was pruned "
            f"({result['retention_days']}-day retention); daily totals are kept."
        )
    visit = result["open_visit"]
    if visit is not None:
        seen = "start seen" if visit["complete_start"] else "already in voice when the bot started watching"
        lines.append(f"In voice now: **yes**, since {_local_time(visit['started_at'], timezone)} ({seen}).")
    elif result["last_voice_at"] is not None:
        lines.append(f"In voice now: no; last seen in voice {_local_time(result['last_voice_at'], timezone)}.")
    else:
        lines.append("In voice now: no; never seen in voice.")
    if result["paused"]:
        lines.append("Collection is paused for everyone right now.")
    return "\n".join(lines)


async def _alerts_text(bot: Any, minutes: int | None) -> str:
    if minutes is not None:
        await bot.store.set_outage_alert_minutes(minutes)
        if minutes == 0:
            return "Outage alerts are now **off**."
        return (
            f"The owner now gets a DM after any outage of **{_duration(minutes * 60)}** or longer, "
            "once the bot is back. Earlier outages are not announced."
        )
    current = await bot.store.outage_alert_minutes()
    if current == 0:
        return "Outage alerts are **off**. Set `minutes` to turn them on."
    return (
        f"The owner gets a DM after any outage of **{_duration(current * 60)}** or longer, "
        "once the bot is back. Set `minutes` to change it, or 0 to turn alerts off."
    )


async def _request_update_text(bot: Any) -> str:
    database_path = getattr(bot.config, "database_path", None)
    if database_path is None:
        return "Automatic updates aren't set up on this bot."
    status = await asyncio.to_thread(update_status.read_status, database_path)
    if status is not None and status.get("result") == "held":
        return (
            "Updates are held at a pinned commit by an operator, so a check would not deploy anything. "
            "Release the hold on the Pi first."
        )
    await asyncio.to_thread(update_status.request_update, database_path)
    return (
        "Update check requested. The Pi checks GitHub within a few seconds "
        "(or at its next scheduled check if on-demand updates aren't installed there). "
        "If there is a new commit, the bot restarts while it installs; `/flock about` shows the result."
    )


class DeleteDataConfirmation(discord.ui.View):
    """Short-lived confirmation restricted to the original invoker.

    Without a target it erases everyone's data; with ``target_id`` only that
    person's. ``target_name`` is already escaped for Markdown.
    """

    def __init__(
        self,
        bot: Any,
        invoker_id: int,
        *,
        timeout: float = 60.0,
        target_id: int | None = None,
        target_name: str = "that person",
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.invoker_id = invoker_id
        self.target_id = target_id
        self.target_name = target_name
        self.message: discord.Message | None = None
        self._confirmation_lock = asyncio.Lock()
        self._processing = False

    async def _is_invoker(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.invoker_id:
            return True
        await _send(
            interaction,
            "Only the person who requested deletion can use these buttons.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Delete tracked data", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button["DeleteDataConfirmation"],
    ) -> None:
        if not await self._is_invoker(interaction):
            return
        if self.is_finished():
            await _send(interaction, "This deletion confirmation has expired or finished.", ephemeral=True)
            return
        if not await _control_ok(interaction, self.bot):
            return
        async with self._confirmation_lock:
            if self._processing or self.is_finished():
                await _send(interaction, "This deletion request is already being handled or has finished.", ephemeral=True)
                return
            self._processing = True
        try:
            # This is a component interaction. Deferring without ``thinking``
            # acknowledges an update to the original ephemeral message, so the
            # confirmation controls can be removed after the database finishes.
            await interaction.response.defer()
            if self.target_id is None:
                existed = True
                await self.bot.tracker.delete_data(actor_id=interaction.user.id)
            else:
                existed = await self.bot.tracker.delete_user_data(
                    self.target_id, interaction.user.id
                )
        except Exception:
            logger.exception("Confirmed data deletion failed")
            self._processing = False
            try:
                await interaction.edit_original_response(
                    content="The tracker could not delete the data. The error was logged. You may retry while this confirmation is open.",
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                await _send(interaction, "The tracker could not delete the data. The error was logged.", ephemeral=True)
            return
        self._processing = False
        self.stop()
        if self.target_id is None:
            content = (
                "Everyone's tracked statistics and managed local backups were deleted. "
                "The tracked list was kept. Collection remains paused."
            )
        elif existed:
            content = (
                f"{self.target_name}'s statistics and managed local backups were deleted, and "
                "they are no longer tracked. Collection for everyone else continues."
            )
        else:
            content = f"No statistics were recorded for {self.target_name}."
        await interaction.edit_original_response(
            content=content,
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button["DeleteDataConfirmation"],
    ) -> None:
        if not await self._is_invoker(interaction):
            return
        if not await _scope_ok(interaction, self.bot):
            return
        async with self._confirmation_lock:
            if self._processing:
                await _send(interaction, "This deletion request is already being handled and cannot be cancelled.", ephemeral=True)
                return
            if self.is_finished():
                await _send(interaction, "This deletion confirmation has expired or finished.", ephemeral=True)
                return
            self.stop()
        await interaction.response.edit_message(
            content="Data deletion cancelled.",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        # discord.py finishes the view before calling this; stopping again is
        # harmless and makes the buttons refuse even if this is called directly.
        self.stop()
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Could not disable an expired data deletion view")

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[Any],
    ) -> None:
        logger.exception("Data deletion confirmation action failed")
        await _send(
            interaction,
            "The tracker could not complete that action. The error was logged.",
            ephemeral=True,
        )


def _async_value(value: str) -> Callable[[], Awaitable[str]]:
    async def result() -> str:
        return value

    return result


def register_commands(bot: Any) -> None:
    """Add the ``/flock`` command group to ``bot.tree`` for the configured guild."""
    guild = discord.Object(id=bot.config.guild_id)
    flock = app_commands.Group(name="flock", description="Activity reports for tracked people and bot controls")
    admin = app_commands.Group(name="admin", description="Manage tracker admins", parent=flock)
    track = app_commands.Group(name="track", description="Manage who is tracked", parent=flock)
    debug = app_commands.Group(
        name="debug", description="Admin-only bot health, uptime, and troubleshooting", parent=flock
    )

    async def person_report(
        interaction: discord.Interaction,
        label: str,
        user: Any,
        build: Callable[[_Person], Awaitable[str | tuple[str, bytes | None]]],
        *,
        ephemeral: bool | None = None,
        require_active: bool = False,
        gate: Callable[[], Awaitable[bool]] | None = None,
        filename: str = "chart.png",
    ) -> None:
        """Resolve the person a report is about, then run and deliver it.

        Replies are public in report channels unless ``ephemeral`` is given.
        ``gate`` runs after the person is resolved and can refuse the report.
        """
        person = await _resolve_person(interaction, bot, label, user, require_active=require_active)
        if person is None or (gate is not None and not await gate()):
            return
        await _run(
            interaction,
            label,
            lambda: build(person),
            ephemeral=_report_is_ephemeral(bot, interaction) if ephemeral is None else ephemeral,
            note=person.note,
            filename=filename,
        )

    async def control(
        interaction: discord.Interaction,
        label: str,
        action: Callable[[], Awaitable[str | tuple[str, bytes | None]]],
        *,
        filename: str = "chart.png",
    ) -> None:
        """Run an admin-only command and reply privately."""
        if await _control_ok(interaction, bot):
            await _run(interaction, label, action, ephemeral=True, filename=filename)

    @flock.command(name="stats", description="Show activity statistics for a period")
    @app_commands.describe(period="The period to summarize", user=_USER_OPTION)
    @app_commands.choices(period=PERIOD_CHOICES)
    async def stats_command(
        interaction: discord.Interaction,
        period: str = "week",
        user: discord.User | None = None,
    ) -> None:
        await person_report(
            interaction, "flock stats", user, lambda person: _stats_text(bot, person, period)
        )

    @flock.command(name="records", description="Show personal activity records")
    @app_commands.describe(user=_USER_OPTION)
    async def records_command(
        interaction: discord.Interaction, user: discord.User | None = None
    ) -> None:
        await person_report(
            interaction, "flock records", user, lambda person: _records_text(bot, interaction, person)
        )

    @flock.command(name="where", description="Show when and where someone was last in voice")
    @app_commands.describe(user=_USER_OPTION)
    async def where_command(
        interaction: discord.Interaction, user: discord.User | None = None
    ) -> None:
        await person_report(
            interaction, "flock where", user, lambda person: _seen_text(bot, interaction, person)
        )

    @flock.command(name="company", description="Chart who shared someone's observed voice time")
    @app_commands.describe(
        period="The period to summarize", count=_COMPANY_COUNT_OPTION, user=_USER_OPTION
    )
    @app_commands.choices(period=PERIOD_CHOICES, count=COMPANY_COUNT_CHOICES)
    async def company_command(
        interaction: discord.Interaction,
        period: str = "week",
        count: str = "split",
        user: discord.User | None = None,
    ) -> None:
        await person_report(
            interaction,
            "flock company",
            user,
            lambda person: _company_report(bot, interaction, person, period, count),
            filename="flock-voice-company.png",
        )

    @flock.command(name="leaderboard", description="Rank who spent the most voice time with someone")
    @app_commands.describe(period="The period to rank (all time by default)", user=_USER_OPTION)
    @app_commands.choices(period=PERIOD_CHOICES)
    async def leaderboard_command(
        interaction: discord.Interaction,
        period: str = "all",
        user: discord.User | None = None,
    ) -> None:
        await person_report(
            interaction,
            "flock leaderboard",
            user,
            lambda person: _leaderboard_text(bot, interaction, person, period),
        )

    @flock.command(name="trends", description="Chart how someone's activity changes over a period")
    @app_commands.describe(
        period="The period to chart",
        kind="Which trend to chart",
        count="Company kind only: how to count time shared with several people (split by default)",
        user=_USER_OPTION,
    )
    @app_commands.choices(period=TREND_PERIOD_CHOICES, kind=TREND_CHOICES, count=COMPANY_COUNT_CHOICES)
    async def trends_command(
        interaction: discord.Interaction,
        period: str = "last7",
        kind: str = "daily",
        count: str = "split",
        user: discord.User | None = None,
    ) -> None:
        await person_report(
            interaction,
            "flock trends",
            user,
            lambda person: _trend_report(bot, interaction, person, period, kind, count),
            filename=f"flock-trends-{kind}.png",
        )

    @flock.command(name="online", description="Check whether someone is online, away, or offline")
    @app_commands.describe(user=_USER_OPTION)
    async def online_command(
        interaction: discord.Interaction, user: discord.User | None = None
    ) -> None:
        await person_report(
            interaction,
            "flock online",
            user,
            lambda person: _online_text(bot, person),
            ephemeral=True,
            require_active=True,
        )

    @flock.command(name="roast", description="Get a light joke based on real activity")
    @app_commands.describe(period="The period to use for the joke", user=_USER_OPTION)
    @app_commands.choices(period=PERIOD_CHOICES)
    async def roast_command(
        interaction: discord.Interaction,
        period: str = "week",
        user: discord.User | None = None,
    ) -> None:
        async def roast(person: _Person) -> str:
            stats = await _read_stats(bot, person.user_id, period, time.time())
            joke = make_roast(stats, _period_label(period), name=person.name)
            if joke is None:
                return f"There is no recorded activity to joke about for {_period_label(period)} yet."
            return joke

        await person_report(
            interaction,
            "flock roast",
            user,
            roast,
            gate=lambda: _cooldown_ok(
                interaction, _ROAST_COOLDOWN, "The shared roast cooldown is active."
            ),
        )

    @flock.command(name="top", description="Rank tracked people by messages, voice time, or active days")
    @app_commands.describe(period="The period to rank", metric="What to rank by (messages by default)")
    @app_commands.choices(period=PERIOD_CHOICES, metric=TOP_METRIC_CHOICES)
    async def top_command(
        interaction: discord.Interaction, period: str = "week", metric: str = "messages"
    ) -> None:
        if await _scope_ok(interaction, bot):
            await _run(
                interaction,
                "flock top",
                lambda: _top_text(bot, period, metric),
                ephemeral=_report_is_ephemeral(bot, interaction),
            )

    @flock.command(name="introduce", description="Have the bot introduce itself in this channel")
    async def introduce_command(interaction: discord.Interaction) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _cooldown_ok(interaction, _INTRODUCE_COOLDOWN, "I just introduced myself."):
            return
        await _run(
            interaction, "flock introduce", _async_value(_introduction_text(bot)), ephemeral=False
        )

    @flock.command(name="help", description="Explain commands and what the bot measures")
    async def help_command(interaction: discord.Interaction) -> None:
        if await _scope_ok(interaction, bot):
            await _run(
                interaction,
                "flock help",
                _async_value(_help_text(bot)),
                ephemeral=_report_is_ephemeral(bot, interaction),
            )

    @flock.command(name="about", description="Show the bot version, connection, and coverage status")
    async def about_command(interaction: discord.Interaction) -> None:
        if await _scope_ok(interaction, bot):
            await _run(interaction, "flock about", lambda: _status_text(bot), ephemeral=True)

    @flock.command(name="version", description="Show the running bot version")
    async def version_command(interaction: discord.Interaction) -> None:
        if await _scope_ok(interaction, bot):
            await _run(
                interaction,
                "flock version",
                _async_value(f"Flock **{version_string()}**."),
                ephemeral=True,
            )

    @admin.command(name="add", description="Grant tracker admin access to a server member")
    @app_commands.describe(user="Server member to grant tracker controls")
    async def admin_add_command(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await _owner_ok(interaction, bot):
            return
        if user.id in (_config(bot).owner_user_id, _leland_id(bot)) or user.bot:
            await _send(
                interaction,
                "Choose a human member other than the owner or the configured Leland user.",
                ephemeral=True,
            )
            return

        async def action() -> str:
            await bot.store.set_admin_override(user.id, True)
            return f"{_user_label(user, user.id)} now has tracker admin access."

        await _run(interaction, "flock admin add", action, ephemeral=True)

    @admin.command(name="remove", description="Revoke tracker admin access by user ID")
    @app_commands.describe(user_id="Discord user ID or mention; works after a member leaves")
    async def admin_remove_command(interaction: discord.Interaction, user_id: str) -> None:
        if not await _owner_ok(interaction, bot):
            return
        parsed_id = _parse_user_id(user_id)
        if parsed_id is None:
            await _send(interaction, "Enter a positive Discord user ID or user mention.", ephemeral=True)
            return
        if parsed_id == _config(bot).owner_user_id:
            await _send(interaction, "The configured owner cannot be removed.", ephemeral=True)
            return

        async def action() -> str:
            await bot.store.set_admin_override(parsed_id, False)
            label = await _admin_label(bot, _guild(bot), parsed_id)
            return f"{label} no longer has tracker admin access."

        await _run(interaction, "flock admin remove", action, ephemeral=True)

    @admin.command(name="list", description="List the current tracker admins")
    async def admin_list_command(interaction: discord.Interaction) -> None:
        if not await _owner_ok(interaction, bot):
            return

        async def action() -> str:
            config = _config(bot)
            overrides = await bot.store.admin_overrides()
            admins = set(config.admin_user_ids)
            for user_id, enabled in overrides.items():
                if enabled:
                    admins.add(user_id)
                else:
                    admins.discard(user_id)
            admins.discard(config.owner_user_id)
            admins.discard(_leland_id(bot))
            guild = _guild(bot)
            owner_label, *admin_labels = await asyncio.gather(*(
                _admin_label(bot, guild, user_id)
                for user_id in (config.owner_user_id, *sorted(admins))
            ))
            lines = [f"Owner: {owner_label}", "Tracker admins:"]
            if not admins:
                lines.append("None")
            omitted = _append_fitting(lines, admin_labels)
            if omitted:
                lines.append(f"…and {omitted} more admins")
            return "\n".join(lines)

        await _run(interaction, "flock admin list", action, ephemeral=True)

    @track.command(name="add", description="Start tracking a server member")
    @app_commands.describe(user="Server member to start tracking")
    async def track_add_command(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await _control_ok(interaction, bot):
            return
        if user.bot:
            await _send(interaction, "Bots can't be tracked.", ephemeral=True)
            return

        async def action() -> str:
            added = await bot.tracker.track_user(user.id, interaction.user.id, bot.voice_snapshot)
            label = _user_label(user, user.id)
            if not added:
                return f"{label} is already tracked."
            reply = f"{label} is now tracked. Their messages and voice time are counted from now."
            if (await bot.store.state()).get("paused", False):
                reply += " Collection is paused, so counting starts when it resumes."
            return reply

        await _run(interaction, "flock track add", action, ephemeral=True)

    @track.command(name="remove", description="Stop tracking someone by user ID, keeping their history")
    @app_commands.describe(user_id="Discord user ID or mention; works after a member leaves")
    async def track_remove_command(interaction: discord.Interaction, user_id: str) -> None:
        if not await _control_ok(interaction, bot):
            return
        parsed_id = _parse_user_id(user_id)
        if parsed_id is None:
            await _send(interaction, "Enter a positive Discord user ID or user mention.", ephemeral=True)
            return

        async def action() -> str:
            removed = await bot.tracker.untrack_user(parsed_id, interaction.user.id)
            label = await _admin_label(bot, _guild(bot), parsed_id)
            if not removed:
                return f"{label} isn't currently tracked."
            return f"{label} is no longer tracked. Their recorded history is kept."

        await _run(interaction, "flock track remove", action, ephemeral=True)

    @track.command(name="list", description="List who is tracked")
    async def track_list_command(interaction: discord.Interaction) -> None:
        if await _scope_ok(interaction, bot):
            await _run(interaction, "flock track list", lambda: _tracked_list_text(bot), ephemeral=True)

    @flock.command(name="update", description="Check GitHub for a new bot version now")
    async def update_command(interaction: discord.Interaction) -> None:
        await control(interaction, "flock update", lambda: _request_update_text(bot))

    @flock.command(name="pause", description="Pause activity collection")
    async def pause_command(interaction: discord.Interaction) -> None:
        async def action() -> str:
            await bot.tracker.pause(actor_id=interaction.user.id)
            return "Collection is paused. This setting persists across restarts."

        await control(interaction, "flock pause", action)

    @flock.command(name="resume", description="Resume activity collection")
    async def resume_command(interaction: discord.Interaction) -> None:
        async def action() -> str:
            await bot.tracker.resume(interaction.user.id, bot.voice_snapshot)
            snapshot = bot.voice_snapshot()
            tracked = getattr(bot.tracker, "tracked_ids", frozenset())
            watching = sum(1 for member_id in snapshot if member_id in tracked)
            if not watching:
                return (
                    "Collection resumed. Voice tracking will start when a tracked person "
                    "joins a tracked voice channel."
                )
            return (
                f"Collection resumed. {_plural(watching, 'tracked person')} currently in voice "
                f"{'is' if watching == 1 else 'are'} being observed from now."
            )

        await control(interaction, "flock resume", action)

    def add_leland_toggle(
        name: str, description: str, mode_description: str, label: str, setter: str
    ) -> None:
        """Add an admin command that turns a Leland-only mode on or off."""

        @flock.command(name=name, description=description)
        @app_commands.describe(mode=mode_description)
        @app_commands.choices(mode=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off"),
        ])
        async def toggle_command(interaction: discord.Interaction, mode: str) -> None:
            if not await _control_ok(interaction, bot, leland=True):
                return

            async def action() -> str:
                await getattr(bot.store, setter)(mode == "on")
                return f"{label} is now **{mode}**."

            await _run(interaction, f"flock {name}", action, ephemeral=True)

    add_leland_toggle(
        "evil-mode",
        "Toggle upside-down reposts of Leland's messages",
        "Turn upside-down reposts on or off",
        "Evil Leland mode",
        "set_evil_mode",
    )
    add_leland_toggle(
        "reaction-mode",
        "Toggle occasional reactions to Leland's messages",
        "Turn occasional emoji reactions on or off",
        "Reaction mode",
        "set_reaction_mode",
    )

    @flock.command(name="delete-data", description="Erase one person's statistics, or everyone's and pause collection")
    @app_commands.describe(
        user="Erase only this person's data (everyone's if both options are omitted)",
        user_id="Erase one person by Discord user ID or mention; works after they leave",
    )
    async def delete_data_command(
        interaction: discord.Interaction,
        user: discord.User | None = None,
        user_id: str | None = None,
    ) -> None:
        if not await _control_ok(interaction, bot):
            return
        if user is not None and user_id is not None:
            await _send(interaction, "Choose either `user` or `user_id`, not both.", ephemeral=True)
            return
        target = user
        target_id: int | None = None if user is None else int(user.id)
        if user_id is not None:
            target_id = _parse_user_id(user_id)
            if target_id is None:
                await _send(interaction, "Enter a Discord user ID or mention.", ephemeral=True)
                return
            # Cache only: this prompt is the interaction's first response, so a
            # Discord lookup could run past the acknowledgement window.
            server = getattr(interaction, "guild", None)
            target = server.get_member(target_id) if server is not None else None
            if target is None and hasattr(bot, "get_user"):
                target = bot.get_user(target_id)
        if getattr(target, "bot", False):
            await _send(interaction, "Bots aren't tracked.", ephemeral=True)
            return
        if target_id is None:
            view = DeleteDataConfirmation(bot, interaction.user.id)
            prompt = (
                "This permanently erases the tracked statistics of **everyone** and all managed "
                "local backups, then pauses collection. The tracked list is kept. Continue?"
            )
        else:
            name = _safe_name(_person_name(target, target_id))
            view = DeleteDataConfirmation(
                bot, interaction.user.id, target_id=target_id, target_name=name
            )
            prompt = (
                f"This permanently erases only **{name}**'s recorded statistics, removes them from "
                "the tracked list, and deletes all managed local backups (they contain that data). "
                "Collection for everyone else continues. Continue?"
            )
        await interaction.response.send_message(
            content=prompt,
            ephemeral=True,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            view.message = await interaction.original_response()
        except (discord.HTTPException, AttributeError):
            # The view still expires even if a test or interaction wrapper does
            # not expose the original response message.
            pass

    @debug.command(name="health", description="Show uptime, checkpoints, errors, storage, and backups")
    async def debug_health_command(interaction: discord.Interaction) -> None:
        await control(interaction, "flock debug health", lambda: _health_text(bot))

    @debug.command(name="uptime", description="Chart when the bot was watching and list its outages")
    @app_commands.describe(period="The period to check (last 7 days by default)")
    @app_commands.choices(period=TREND_PERIOD_CHOICES)
    async def debug_uptime_command(interaction: discord.Interaction, period: str = "last7") -> None:
        await control(
            interaction, "flock debug uptime", lambda: _uptime_report(bot, period),
            filename="flock-uptime.png",
        )

    @debug.command(name="errors", description="List recent warnings and errors the bot logged")
    async def debug_errors_command(interaction: discord.Interaction) -> None:
        await control(interaction, "flock debug errors", lambda: _errors_text(bot))

    @debug.command(name="person", description="Show one person's tracking history, coverage, and stored data")
    @app_commands.describe(user="The person to inspect")
    async def debug_person_command(interaction: discord.Interaction, user: discord.User) -> None:
        if not await _control_ok(interaction, bot):
            return
        if getattr(user, "bot", False):
            await _send(interaction, "Bots aren't tracked.", ephemeral=True)
            return
        await _run(
            interaction,
            "flock debug person",
            lambda: _person_debug_text(bot, interaction, user),
            ephemeral=True,
        )

    @debug.command(name="alerts", description="Show or set when the owner is DMed about an outage")
    @app_commands.describe(minutes="Alert after outages at least this long; 0 turns alerts off")
    async def debug_alerts_command(
        interaction: discord.Interaction,
        minutes: app_commands.Range[int, 0, 1440] | None = None,
    ) -> None:
        await control(interaction, "flock debug alerts", lambda: _alerts_text(bot, minutes))

    bot.tree.add_command(flock, guild=guild)

    async def on_tree_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        command = getattr(interaction, "command", None)
        logger.error(
            "Slash command %s failed before completion",
            getattr(command, "qualified_name", "unknown"),
            exc_info=(type(error), error, error.__traceback__),
        )
        await _send(interaction, _FAILURE_TEXT, ephemeral=True)

    bot.tree.on_error = on_tree_error
