"""Guild-scoped slash commands for the tracker."""

from __future__ import annotations

import asyncio
from io import BytesIO
import logging
import math
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont

from . import update_status, version_string
from .jokes import SharedRoastCooldown, make_roast

logger = logging.getLogger(__name__)

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
# Trends default to a rolling window so early in the week the chart still has data.
TREND_PERIOD_CHOICES = [app_commands.Choice(name="Last 7 days", value="last7"), *PERIOD_CHOICES]

_ROAST_COOLDOWN = SharedRoastCooldown(seconds=30)
_FAILURE_TEXT = "The tracker could not complete that request. The error was logged."
# Categorical palette in fixed slot order, validated for colour-vision deficiency.
_PIE_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948")


def _config(bot: Any) -> Any:
    return bot.config


def _period_label(period: str) -> str:
    return PERIOD_LABELS.get(period, "this period")


def _local_time(timestamp: float | None, timezone: str, *, date_only: bool = False) -> str:
    if timestamp is None:
        return "not recorded"
    local = datetime.fromtimestamp(float(timestamp), tz=ZoneInfo(timezone))
    if date_only:
        return local.strftime("%b %-d, %Y")
    return local.strftime("%b %-d, %Y %H:%M %Z")


def _duration(seconds: float) -> str:
    whole = max(0, int(seconds))
    days, remain = divmod(whole, 86_400)
    hours, remain = divmod(remain, 3_600)
    minutes, secs = divmod(remain, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    if not parts or (not days and not hours and not minutes):
        parts.append(f"{secs}s")
    return " ".join(parts)


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
    if user_id == config.target_user_id:
        return False
    override = await bot.store.admin_override(user_id)
    return user_id in config.admin_user_ids if override is None else override


async def _control_permission_ok(
    interaction: discord.Interaction,
    bot: Any,
) -> bool:
    if await _can_control(interaction, bot):
        return True
    await _send(
        interaction,
        "Only configured tracker admins can change tracker settings.",
        ephemeral=True,
    )
    return False


async def _owner_permission_ok(interaction: discord.Interaction, bot: Any) -> bool:
    if interaction.user.id == _config(bot).owner_user_id:
        return True
    await _send(
        interaction,
        "Only the configured tracker owner can manage admins.",
        ephemeral=True,
    )
    return False


def _admin_id(raw: str) -> int | None:
    match = re.fullmatch(r"(?:<@!?(\d+)>|(\d+))", raw.strip())
    if match is None:
        return None
    user_id = int(match.group(1) or match.group(2))
    return user_id if user_id > 0 else None


async def _execute(
    interaction: discord.Interaction,
    bot: Any,
    label: str,
    action: Callable[[], Awaitable[str]],
    *,
    ephemeral: bool,
) -> None:
    if not await _scope_ok(interaction, bot):
        return
    await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    try:
        content = await action()
    except Exception:
        logger.exception("Slash command %s failed", label)
        content = "The tracker could not complete that request. The error was logged."
    await _send(interaction, content, ephemeral=ephemeral)


async def _stats_text(bot: Any, period: str) -> str:
    now = time.time()
    result = await _read_stats(bot, period, now)
    timezone = bot.config.timezone
    label = _period_label(period)
    tracking_since = _local_time(result["tracking_since"], timezone, date_only=True)
    lines = [f"**Leland's activity — {label}**"]
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
    tracker = getattr(bot, "tracker", None)
    if tracker is not None:
        if not bool(getattr(tracker, "connected", True)):
            lines.append("Collection is currently unavailable because the bot is disconnected.")
        elif not bool(getattr(tracker, "guild_is_available", True)):
            lines.append("Collection is currently unavailable because the configured server is unavailable.")
        elif not bool(getattr(tracker, "collection_ready", True)):
            lines.append("Collection is recovering. Voice time includes only saved observations until recovery finishes.")
    return "\n".join(lines)


async def _read_stats(bot: Any, period: str, now: float) -> dict[str, Any]:
    tracker = bot.tracker
    reliable = all(bool(getattr(tracker, name, True)) for name in (
        "connected", "guild_is_available", "collection_ready"
    ))
    return await bot.store.stats(period, now, include_live=reliable)


async def _seen_text(bot: Any, interaction: discord.Interaction) -> str:
    now = time.time()
    tracker = bot.tracker
    reliable = all(bool(getattr(tracker, name, True)) for name in (
        "connected", "guild_is_available", "collection_ready"
    ))
    observation = await bot.store.last_voice(now, include_live=reliable)
    if observation is None:
        return "I haven't observed Leland in a tracked voice channel yet."

    channel_id = int(observation["channel_id"])
    channel = bot.get_channel(channel_id)
    channel_label = "a voice channel"
    if channel is not None:
        # A public reply must not reveal a channel name visible only to the caller.
        if _report_is_ephemeral(bot, interaction):
            viewer = interaction.user
        else:
            viewer = getattr(getattr(interaction, "guild", None), "default_role", None)
        try:
            can_view = viewer is not None and bool(channel.permissions_for(viewer).view_channel)
        except (AttributeError, TypeError):
            can_view = False
        if can_view:
            name = discord.utils.escape_markdown(str(channel.name))
            channel_label = f"**#{name}**"

    if observation["current"]:
        since = _local_time(observation["observed_since"], bot.config.timezone)
        return f"Leland is currently in {channel_label}. I've observed him there since {since}."

    seen_at = float(observation["seen_at"])
    elapsed = _duration(max(0.0, now - seen_at))
    date = _local_time(seen_at, bot.config.timezone)
    return f"Leland was last seen in {channel_label} **{elapsed} ago** ({date})."


def _pie_png(slices: list[tuple[str, float]]) -> bytes:
    image = Image.new("RGB", (1100, 640), "#ffffff")
    draw = ImageDraw.Draw(image)
    try:
        title_font = ImageFont.truetype("DejaVuSans.ttf", 38)
        label_font = ImageFont.truetype("DejaVuSans.ttf", 32)
        detail_font = ImageFont.truetype("DejaVuSans.ttf", 24)
    except OSError:
        title_font = ImageFont.load_default(size=38)
        label_font = ImageFont.load_default(size=32)
        detail_font = ImageFont.load_default(size=24)
    draw.text((44, 28), "Leland's voice company", font=title_font, fill="#17212f")
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
        draw.text(
            (660, legend_y + 36), f"{seconds / total:.1%}  ·  {_duration(seconds)}",
            font=detail_font, fill="#48576b",
        )
        angle = next_angle
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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


def _clean_name(value: Any) -> str:
    return " ".join(str(value).split())[:48] if value else ""


async def _company_name(bot: Any, guild: Any, member_id: int) -> str:
    if member_id == 0:
        return "Alone"
    if member_id == -1:
        return "Other people"
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
    safe = discord.utils.escape_mentions(discord.utils.escape_markdown(label))
    return f"{safe} — {user_id}"


async def _admin_label(bot: Any, guild: Any, user_id: int) -> str:
    return _user_label(await _lookup_user(bot, guild, user_id), user_id)


def _visible_company_rows(
    bot: Any, interaction: discord.Interaction, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep positive company rows from voice channels the report audience can view."""
    viewer = (
        interaction.user if _report_is_ephemeral(bot, interaction)
        else getattr(getattr(interaction, "guild", None), "default_role", None)
    )
    visible_rows = []
    for row in rows:
        if float(row["seconds"]) <= 0:
            continue
        channel = bot.get_channel(int(row["channel_id"]))
        try:
            visible = viewer is not None and bool(channel.permissions_for(viewer).view_channel)
        except (AttributeError, TypeError):
            visible = False
        if visible:
            visible_rows.append(row)
    return visible_rows


async def _company_seconds(
    bot: Any, interaction: discord.Interaction, period: str, field: str = "seconds"
) -> dict[int, float]:
    """Return seconds by peer (0 means alone) in channels the audience can view.

    ``field`` is ``seconds`` for the even split or ``full_seconds`` for whole shared time.
    """
    tracker = bot.tracker
    reliable = all(bool(getattr(tracker, name, True)) for name in (
        "connected", "guild_is_available", "collection_ready"
    ))
    rows = await bot.store.company_totals(period, time.time(), include_live=reliable)
    seconds_by_member: dict[int, float] = {}
    for row in _visible_company_rows(bot, interaction, rows):
        seconds = float(row.get(field, 0.0))
        if seconds <= 0:
            continue
        member_id = int(row["member_id"])
        seconds_by_member[member_id] = seconds_by_member.get(member_id, 0.0) + seconds
    return seconds_by_member


async def _company_report(
    bot: Any, interaction: discord.Interaction, period: str
) -> tuple[str, bytes | None]:
    seconds_by_member = await _company_seconds(bot, interaction, period)

    label = _period_label(period)
    if not seconds_by_member:
        return (
            f"No companion time has been observed for {label} in voice channels visible to this report. "
            "Companion tracking starts with this update; earlier voice time cannot be reconstructed.",
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
    if len(ranked_peers) > limit:
        top.append((-1, sum(seconds for _, seconds in ranked_peers[limit:])))
    guild = bot.get_guild(bot.config.guild_id)
    names = await asyncio.gather(*(
        _company_name(bot, guild, member_id) for member_id, _ in top
    ))
    slices = [(name, seconds) for name, (_, seconds) in zip(names, top)]
    total = sum(seconds_by_member.values())
    lines = [
        f"**Leland's voice company — {label}**",
        f"Observed time in visible channels: **{_duration(total)}**.",
    ]
    for index, (name, seconds) in enumerate(slices):
        safe_name = discord.utils.escape_mentions(discord.utils.escape_markdown(name))
        lines.append(
            f"{index + 1}. **{safe_name}** — {_duration(seconds)} ({seconds / total:.1%})"
        )
    lines.append(
        "Each shared minute is split evenly among the people present; time alone has its own slice. "
        "Only observed time since companion tracking began is included."
    )
    return "\n".join(lines), _pie_png(slices)


_LEADERBOARD_SIZE = 10
_LEADERBOARD_MEDALS = ("🥇", "🥈", "🥉")


async def _leaderboard_text(bot: Any, interaction: discord.Interaction, period: str) -> str:
    """Rank people by the whole voice time they shared with Leland."""
    full_by_member = await _company_seconds(bot, interaction, period, "full_seconds")
    label = _period_label(period)
    ranked = sorted(
        ((member_id, seconds) for member_id, seconds in full_by_member.items() if member_id != 0),
        key=lambda item: (-item[1], item[0]),
    )
    if not ranked:
        return (
            f"No time with other people has been observed for {label} in voice channels visible to this report. "
            "Only observed time since companion tracking began is counted."
        )
    top = ranked[:_LEADERBOARD_SIZE]
    guild = bot.get_guild(bot.config.guild_id)
    names = await asyncio.gather(*(_company_name(bot, guild, member_id) for member_id, _ in top))
    lines = [f"**Leland's voice leaderboard — {label}**"]
    for index, (name, (_, seconds)) in enumerate(zip(names, top)):
        rank = _LEADERBOARD_MEDALS[index] if index < len(_LEADERBOARD_MEDALS) else f"{index + 1}."
        safe_name = discord.utils.escape_mentions(discord.utils.escape_markdown(name))
        lines.append(f"{rank} **{safe_name}** — {_duration(seconds)}")
    if len(ranked) > len(top):
        lines.append(f"…and {_plural(len(ranked) - len(top), 'other')}.")
    if full_by_member.get(0, 0.0) > 0:
        lines.append(f"Time alone (not ranked): {_duration(full_by_member[0])}.")
    lines.append(
        "Each person gets the whole time they were in a tracked voice channel with Leland, so "
        "group calls count fully for everyone. Time recorded before that change counts as its "
        "even split, as in `/leland company`."
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


def _collection_reliable(bot: Any) -> bool:
    tracker = bot.tracker
    return all(bool(getattr(tracker, name, True)) for name in (
        "connected", "guild_is_available", "collection_ready"
    ))


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


def _daily_trend(series: list[dict[str, Any]], label: str) -> tuple[str, bytes]:
    unit, labels, messages, voice = _bucket_series(series)
    busiest = max(series, key=lambda entry: (int(entry["messages"]), float(entry["voice_seconds"])))
    active = sum(1 for entry in series if _is_active(entry))
    observed = sum(1 for entry in series if _is_observed(entry))
    longest, current = _streaks(series)
    ghosts, ghost_run, ghost_start, ghost_end = _ghost_days(series)
    lines = [
        f"**Leland's day-by-day trend — {label}**",
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
        f"Leland's activity per {unit} — {label}",
        labels,
        [
            (f"Messages per {unit}", messages, _PIE_COLORS[0], "count"),
            (f"Observed voice time per {unit}", voice, _PIE_COLORS[1], "duration"),
        ],
    )
    return "\n".join(lines), png


def _weekday_trend(series: list[dict[str, Any]], label: str) -> tuple[str, bytes]:
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
    lines = [f"**Leland's week pattern — {label}**"]
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
        f"Leland's average day of the week — {label}",
        list(_WEEKDAYS),
        [
            ("Average messages per day", avg_messages, _PIE_COLORS[0], "average"),
            ("Average observed voice time per day", avg_voice, _PIE_COLORS[1], "duration"),
        ],
    )
    return "\n".join(lines), png


def _hour_trend(
    messages: dict[str, Any], voice: dict[str, Any], label: str, timezone: str
) -> tuple[str, bytes | None]:
    zone = ZoneInfo(timezone)
    message_hours = [0.0] * 24
    for created_at in messages["times"]:
        message_hours[datetime.fromtimestamp(float(created_at), tz=zone).hour] += 1
    voice_hours = [float(value) for value in voice["hours"]]
    message_total, voice_total = sum(message_hours), sum(voice_hours)
    if message_total == 0 and voice_total == 0:
        return f"No messages or voice time with retained times have been recorded for {label}.", None
    lines = [f"**Leland's clock — {label}**"]
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
        f"Leland by hour of day — {label}",
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


def _compare_trend(result: dict[str, Any], period: str, label: str) -> tuple[str, bytes | None]:
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
        f"**Leland vs {previous_label} — {label} so far**",
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
        f"Leland vs {previous_label} — same point in time",
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


def _burst_trend(result: dict[str, Any], label: str, timezone: str) -> tuple[str, bytes | None]:
    bursts = _bursts(sorted(float(value) for value in result["times"]))
    if not bursts:
        return f"No messages with retained send times have been recorded for {label}.", None
    total = sum(count for _, _, count in bursts)
    biggest = max(bursts, key=lambda burst: (burst[2], -burst[0]))
    rapid = sum(count for _, _, count in bursts if count >= 5)
    lines = [
        f"**Leland's message bursts — {label}**",
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
        f"Leland's burst sizes — {label}",
        [name for _, _, name in _BURST_BINS],
        [("Bursts, by messages in each burst", counts, _PIE_COLORS[0], "count")],
    )
    return "\n".join(lines), png


async def _company_trend(
    bot: Any, interaction: discord.Interaction, period: str, label: str
) -> tuple[str, bytes | None]:
    now = time.time()
    rows = await bot.store.company_daily(period, now, include_live=_collection_reliable(bot))
    rows = _visible_company_rows(bot, interaction, rows)
    if not rows:
        return (
            f"No companion time has been observed for {label} in voice channels visible to this report.",
            None,
        )
    # Buckets run from the first day with company data, which started with that feature.
    first = min(date.fromisoformat(row["day"]) for row in rows)
    last = datetime.fromtimestamp(now, tz=ZoneInfo(bot.config.timezone)).date()
    last = max(last, max(date.fromisoformat(row["day"]) for row in rows))
    day_count = (last - first).days + 1
    unit = _bucket_unit(day_count)
    keys = sorted({_bucket_key(first + timedelta(days=offset), unit) for offset in range(day_count)})
    position = {key: index for index, key in enumerate(keys)}
    by_member: dict[int, list[float]] = {}
    for row in rows:
        values = by_member.setdefault(int(row["member_id"]), [0.0] * len(keys))
        values[position[_bucket_key(date.fromisoformat(row["day"]), unit)]] += float(row["seconds"])
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
    guild = bot.get_guild(bot.config.guild_id)
    names = await asyncio.gather(*(_company_name(bot, guild, member_id) for member_id in named))
    name_of = dict(zip(named, names))
    lines = [f"**Leland's company over time — {label}**"]
    lines.append(f"Top companion each {unit} (most recent last):")
    for index, key in recent:
        best = winners[index]
        when = _bucket_label(key, unit, day_count)
        if best is None:
            alone = by_member.get(0, [0.0] * len(keys))[index]
            lines.append(f"{when}: {'alone — ' + _duration(alone) if alone > 0 else 'no company time'}")
        else:
            safe = discord.utils.escape_mentions(discord.utils.escape_markdown(name_of[best]))
            lines.append(f"{when}: **{safe}** — {_duration(by_member[best][index])}")
    lines.append(
        "Each shared minute is split evenly among the people present; only observed time since "
        "companion tracking began is included."
    )
    png = _stacked_png(
        f"Leland's company per {unit} — {label}",
        f"Observed voice time per {unit}, by companion",
        [_bucket_label(key, unit, day_count) for key in keys],
        [
            (name_of[member_id], values, _PIE_COLORS[index])
            for index, (member_id, values) in enumerate(stacks)
        ],
    )
    return "\n".join(lines), png


async def _trend_report(
    bot: Any, interaction: discord.Interaction, period: str, kind: str
) -> tuple[str, bytes | None]:
    now = time.time()
    label = _period_label(period)
    timezone = bot.config.timezone
    reliable = _collection_reliable(bot)
    if kind == "hours":
        messages = await bot.store.message_times(period, now)
        voice = await bot.store.voice_hours(period, now, include_live=reliable)
        return _hour_trend(messages, voice, label, timezone)
    if kind == "bursts":
        return _burst_trend(await bot.store.message_times(period, now), label, timezone)
    if kind == "compare":
        return _compare_trend(
            await bot.store.period_comparison(period, now, include_live=reliable), period, label
        )
    if kind == "company":
        return await _company_trend(bot, interaction, period, label)
    series = await bot.store.daily_trend(period, now, include_live=reliable)
    if not any(_is_active(entry) for entry in series):
        return f"No activity has been recorded for {label}, so there is no trend to chart.", None
    if kind == "weekdays":
        return _weekday_trend(series, label)
    return _daily_trend(series, label)


async def _online_text(bot: Any) -> str:
    """Request the target's current guild presence without keeping a history."""
    guild = bot.get_guild(bot.config.guild_id)
    if guild is None or getattr(guild, "unavailable", False):
        return "I can't check Leland's Discord status while the server is unavailable."
    try:
        members = await asyncio.wait_for(
            guild.query_members(
                user_ids=[bot.config.target_user_id], presences=True, cache=False
            ),
            timeout=10,
        )
    except (TimeoutError, discord.ClientException):
        return "I couldn't check Leland's Discord status right now. Try again shortly."
    member = next((item for item in members if item.id == bot.config.target_user_id), None)
    if member is None:
        return "I couldn't find Leland in this server to check his Discord status."
    status = member.status
    if status == discord.Status.online:
        return "Leland is online right now."
    if status == discord.Status.idle:
        return "Leland is online (away) right now."
    if status == discord.Status.dnd:
        return "Leland is online (Do Not Disturb) right now."
    if status in (discord.Status.offline, discord.Status.invisible):
        return "Leland appears offline or invisible right now."
    return "I couldn't determine Leland's Discord status right now."


async def _records_text(bot: Any, interaction: discord.Interaction) -> str:
    now = time.time()
    tracker = bot.tracker
    reliable = all(bool(getattr(tracker, name, True)) for name in (
        "connected", "guild_is_available", "collection_ready"
    ))
    records = await bot.store.records(now, include_live=reliable)
    state = await bot.store.state()
    timezone = bot.config.timezone
    started = _local_time(state["tracking_since"], timezone, date_only=True)
    lines = ["**Leland's personal records**", f"Measurement period: since {started} ({timezone})."]

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
        for member_id, seconds in (await _company_seconds(bot, interaction, "all")).items()
        if member_id != 0
    ]
    if peers:
        member_id, seconds = min(peers, key=lambda item: (-item[1], item[0]))
        guild = bot.get_guild(bot.config.guild_id)
        name = discord.utils.escape_mentions(
            discord.utils.escape_markdown(await _company_name(bot, guild, member_id))
        )
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


def _help_text(bot: Any) -> str:
    timezone = bot.config.timezone
    return "\n".join(
        [
            "**Leland Tracker commands**",
            "`/leland stats` — message totals, observed voice time, active days, visits, and coverage gaps.",
            "Stats periods: today, week (default), month, or all time.",
            "`/leland records` — busiest message day, longest fully observed voice visit, "
            "and top voice companion.",
            "`/leland where` — last observed voice channel and time.",
            "`/leland company` — pie chart of observed voice time attributed to companions or time alone.",
            "`/leland leaderboard` — ranks who spent the most observed voice time with Leland, counting the whole time in group calls (all time by default).",
            "`/leland trends` — charts how activity changes: day by day with streaks and ghost days, compared with last period, time of day, day of week, company over time, or message bursts (the last 7 days by default).",
            "`/leland online` — current Discord status; away counts as online.",
            "`/leland roast` — a light joke based on a recorded statistic.",
            "`/leland help` — show this command guide.",
            "`/leland about` — bot version, connection, collection, checkpoint, and coverage status.",
            "`/leland version` — release number and deployed commit of the running bot.",
            "`/leland update` — configured tracker admins can make the Pi check GitHub for a new version now.",
            "`/leland pause` and `/leland resume` — configured tracker admins can pause or resume collection.",
            "`/leland delete-data` — configured tracker admins can erase tracked statistics after confirmation.",
            "`/leland evil-mode mode:on/off` — configured tracker admins can toggle upside-down message reposts.",
            "`/leland reaction-mode mode:on/off` — configured tracker admins can toggle occasional 😂 or 👸 reactions.",
            "`/leland admin add`, `remove`, and `list` — the configured owner manages tracker admins privately.",
            f"Reports use the **{timezone}** timezone. Message counts use message creation events; voice time means observed connection time, not speaking time.",
            "Collection is limited to the configured server and configured visible channels. No message text is stored.",
        ]
    )


async def _status_text(bot: Any) -> str:
    state = await bot.store.state()
    timezone = bot.config.timezone
    tracker = bot.tracker
    connected = bool(getattr(tracker, "connected", False))
    guild_available = bool(getattr(tracker, "guild_is_available", True))
    paused = bool(state.get("paused", False))
    last_checkpoint = state.get("last_checkpoint")
    health = "connected" if connected else "disconnected"
    if not guild_available:
        health += "; configured server unavailable"
    if paused:
        collection_state = "paused"
    elif not connected or not guild_available or not bool(getattr(tracker, "collection_ready", True)):
        collection_state = "unavailable"
    else:
        collection_state = "running"
    lines = [
        "**About Leland Tracker**",
        f"Version: **{version_string()}**.",
        f"Gateway: **{health}**.",
        f"Collection: **{collection_state}**.",
        f"Evil Leland mode: **{'on' if state.get('evil_mode', False) else 'off'}**.",
        f"Reaction mode: **{'on' if state.get('reaction_mode', False) else 'off'}**.",
        f"Message collector: **{'paused' if paused else ('enabled' if collection_state == 'running' else 'unavailable')}**; voice collector: **{'paused' if paused else ('enabled' if collection_state == 'running' else 'unavailable')}**.",
        f"Last checkpoint: {_local_time(last_checkpoint, timezone)}.",
    ]
    try:
        all_stats = await _read_stats(bot, "all", time.time())
        gaps = float(all_stats.get("gap_seconds", 0) or 0)
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
        lines.append("Last tracker error: **recorded**; inspect the service logs for details.")
    if paused and state.get("paused_by") is not None:
        lines.append("Pause remains in effect across restarts.")
    return "\n".join(lines)


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
        "If there is a new commit, the bot restarts while it installs; `/leland about` shows the result."
    )


class DeleteDataConfirmation(discord.ui.View):
    """Short-lived confirmation restricted to the original invoker."""

    def __init__(self, bot: Any, invoker_id: int, *, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.invoker_id = invoker_id
        self.message: discord.Message | None = None
        self._confirmation_lock = asyncio.Lock()
        self._processing = False
        self._completed = False
        self._expired = False

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
        if self._expired or self._completed or self.is_finished():
            await _send(interaction, "This deletion confirmation has expired or finished.", ephemeral=True)
            return
        if not await _scope_ok(interaction, self.bot):
            return
        if not await _control_permission_ok(interaction, self.bot):
            return
        async with self._confirmation_lock:
            if self._processing or self._completed or self._expired:
                await _send(interaction, "This deletion request is already being handled or has finished.", ephemeral=True)
                return
            self._processing = True
        try:
            # This is a component interaction. Deferring without ``thinking``
            # acknowledges an update to the original ephemeral message, so the
            # confirmation controls can be removed after the database finishes.
            await interaction.response.defer()
            await self.bot.tracker.delete_data(actor_id=interaction.user.id)
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
        self._completed = True
        self.stop()
        await interaction.edit_original_response(
            content="Tracked statistics and managed local backups were deleted. Collection remains paused.",
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
        self.stop()
        await interaction.response.edit_message(
            content="Data deletion cancelled.",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        self._expired = True
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


def register_commands(bot: Any) -> None:
    """Add the ``/leland`` command group to ``bot.tree`` for the configured guild."""
    guild = discord.Object(id=bot.config.guild_id)
    leland = app_commands.Group(name="leland", description="Leland's activity reports and bot controls")
    admin = app_commands.Group(name="admin", description="Manage tracker admins", parent=leland)

    @leland.command(name="stats", description="Show activity statistics for a period")
    @app_commands.describe(period="The period to summarize")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def stats_command(
        interaction: discord.Interaction,
        period: str = "week",
    ) -> None:
        await _execute(
            interaction,
            bot,
            "leland stats",
            lambda: _stats_text(bot, period),
            ephemeral=_report_is_ephemeral(bot, interaction),
        )

    @leland.command(name="records", description="Show personal activity records")
    async def records_command(interaction: discord.Interaction) -> None:
        await _execute(
            interaction,
            bot,
            "leland records",
            lambda: _records_text(bot, interaction),
            ephemeral=_report_is_ephemeral(bot, interaction),
        )

    @leland.command(name="where", description="Show when and where Leland was last in voice")
    async def where_command(interaction: discord.Interaction) -> None:
        await _execute(
            interaction,
            bot,
            "leland where",
            lambda: _seen_text(bot, interaction),
            ephemeral=_report_is_ephemeral(bot, interaction),
        )

    @leland.command(name="company", description="Chart who shared Leland's observed voice time")
    @app_commands.describe(period="The period to summarize")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def company_command(
        interaction: discord.Interaction, period: str = "week"
    ) -> None:
        if not await _scope_ok(interaction, bot):
            return
        ephemeral = _report_is_ephemeral(bot, interaction)
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        try:
            content, png = await _company_report(bot, interaction, period)
            file = discord.File(BytesIO(png), filename="leland-voice-company.png") if png else None
        except Exception:
            logger.exception("Slash command leland company failed")
            content = "The tracker could not complete that request. The error was logged."
            file = None
        await _send(interaction, content, ephemeral=ephemeral, file=file)

    @leland.command(name="leaderboard", description="Rank who spent the most voice time with Leland")
    @app_commands.describe(period="The period to rank (all time by default)")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def leaderboard_command(
        interaction: discord.Interaction, period: str = "all"
    ) -> None:
        await _execute(
            interaction,
            bot,
            "leland leaderboard",
            lambda: _leaderboard_text(bot, interaction, period),
            ephemeral=_report_is_ephemeral(bot, interaction),
        )

    @leland.command(name="trends", description="Chart how Leland's activity changes over a period")
    @app_commands.describe(period="The period to chart", kind="Which trend to chart")
    @app_commands.choices(period=TREND_PERIOD_CHOICES, kind=TREND_CHOICES)
    async def trends_command(
        interaction: discord.Interaction, period: str = "last7", kind: str = "daily"
    ) -> None:
        if not await _scope_ok(interaction, bot):
            return
        ephemeral = _report_is_ephemeral(bot, interaction)
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        try:
            content, png = await _trend_report(bot, interaction, period, kind)
            file = discord.File(BytesIO(png), filename=f"leland-trends-{kind}.png") if png else None
        except Exception:
            logger.exception("Slash command leland trends failed")
            content = _FAILURE_TEXT
            file = None
        await _send(interaction, content, ephemeral=ephemeral, file=file)

    @leland.command(name="online", description="Check whether Leland is online, away, or offline")
    async def online_command(interaction: discord.Interaction) -> None:
        await _execute(
            interaction,
            bot,
            "leland online",
            lambda: _online_text(bot),
            ephemeral=True,
        )

    @leland.command(name="roast", description="Get a light joke based on real activity")
    @app_commands.describe(period="The period to use for the joke")
    @app_commands.choices(period=PERIOD_CHOICES)
    async def roast_command(
        interaction: discord.Interaction,
        period: str = "week",
    ) -> None:
        if not await _scope_ok(interaction, bot):
            return
        remaining = await _ROAST_COOLDOWN.consume()
        if remaining > 0:
            await _send(
                interaction,
                f"The shared roast cooldown is active. Try again in {max(1, int(remaining + 0.99))} seconds.",
                ephemeral=True,
            )
            return

        async def action() -> str:
            stats = await _read_stats(bot, period, time.time())
            joke = make_roast(stats, _period_label(period))
            if joke is None:
                return f"There is no recorded activity to joke about for {_period_label(period)} yet."
            return joke

        await _execute_after_scope(
            interaction,
            bot,
            "leland roast",
            action,
            ephemeral=_report_is_ephemeral(bot, interaction),
        )

    @leland.command(name="help", description="Explain commands and what the bot measures")
    async def help_command(interaction: discord.Interaction) -> None:
        await _execute(
            interaction,
            bot,
            "leland help",
            _async_value(_help_text(bot)),
            ephemeral=_report_is_ephemeral(bot, interaction),
        )

    @leland.command(name="about", description="Show the bot version, connection, and coverage status")
    async def about_command(interaction: discord.Interaction) -> None:
        await _execute(
            interaction,
            bot,
            "leland about",
            lambda: _status_text(bot),
            ephemeral=True,
        )

    @leland.command(name="version", description="Show the running bot version")
    async def version_command(interaction: discord.Interaction) -> None:
        await _execute(
            interaction,
            bot,
            "leland version",
            _async_value(f"Leland Tracker **{version_string()}**."),
            ephemeral=True,
        )

    @admin.command(name="add", description="Grant tracker admin access to a server member")
    @app_commands.describe(user="Server member to grant tracker controls")
    async def admin_add_command(interaction: discord.Interaction, user: discord.Member) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _owner_permission_ok(interaction, bot):
            return
        config = _config(bot)
        if user.id in (config.owner_user_id, config.target_user_id) or user.bot:
            await _send(interaction, "Choose a human member other than the owner or tracked user.", ephemeral=True)
            return

        async def action() -> str:
            await bot.store.set_admin_override(user.id, True)
            return f"{_user_label(user, user.id)} now has tracker admin access."

        await _execute_after_scope(interaction, bot, "leland admin add", action, ephemeral=True)

    @admin.command(name="remove", description="Revoke tracker admin access by user ID")
    @app_commands.describe(user_id="Discord user ID or mention; works after a member leaves")
    async def admin_remove_command(interaction: discord.Interaction, user_id: str) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _owner_permission_ok(interaction, bot):
            return
        parsed_id = _admin_id(user_id)
        if parsed_id is None:
            await _send(interaction, "Enter a positive Discord user ID or user mention.", ephemeral=True)
            return
        config = _config(bot)
        if parsed_id == config.owner_user_id:
            await _send(interaction, "The configured owner cannot be removed.", ephemeral=True)
            return

        async def action() -> str:
            await bot.store.set_admin_override(parsed_id, False)
            guild = bot.get_guild(config.guild_id) if hasattr(bot, "get_guild") else None
            label = await _admin_label(bot, guild, parsed_id)
            return f"{label} no longer has tracker admin access."

        await _execute_after_scope(interaction, bot, "leland admin remove", action, ephemeral=True)

    @admin.command(name="list", description="List the current tracker admins")
    async def admin_list_command(interaction: discord.Interaction) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _owner_permission_ok(interaction, bot):
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
            admins.discard(config.target_user_id)
            guild = bot.get_guild(config.guild_id) if hasattr(bot, "get_guild") else None
            ordered = sorted(admins)
            owner_label, *admin_labels = await asyncio.gather(*(
                _admin_label(bot, guild, user_id)
                for user_id in (config.owner_user_id, *ordered)
            ))
            lines = [f"Owner: {owner_label}", "Tracker admins:"]
            if not admins:
                lines.append("None")
            else:
                for index, label in enumerate(admin_labels):
                    candidate = "\n".join((*lines, label))
                    if len(candidate) > 1850:
                        lines.append(f"…and {len(ordered) - index} more admins")
                        break
                    lines.append(label)
            return "\n".join(lines)

        await _execute_after_scope(interaction, bot, "leland admin list", action, ephemeral=True)

    @leland.command(name="update", description="Check GitHub for a new bot version now")
    async def update_command(interaction: discord.Interaction) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _control_permission_ok(interaction, bot):
            return

        async def action() -> str:
            return await _request_update_text(bot)

        await _execute_after_scope(interaction, bot, "leland update", action, ephemeral=True)

    @leland.command(name="pause", description="Pause activity collection")
    async def pause_command(interaction: discord.Interaction) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _control_permission_ok(interaction, bot):
            return

        async def action() -> str:
            await bot.tracker.pause(actor_id=interaction.user.id)
            return "Collection is paused. This setting persists across restarts."

        await _execute_after_scope(interaction, bot, "leland pause", action, ephemeral=True)

    @leland.command(name="resume", description="Resume activity collection")
    async def resume_command(interaction: discord.Interaction) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _control_permission_ok(interaction, bot):
            return

        async def action() -> str:
            channel_id = bot.current_voice_channel_id()
            await bot.tracker.resume(
                actor_id=interaction.user.id,
                voice_channel_id=channel_id,
                companions=bot.current_voice_companions(),
            )
            if channel_id is None:
                return "Collection resumed. Voice tracking will start if Leland joins a tracked voice channel."
            return "Collection resumed. Leland's current voice connection is being observed from now."

        await _execute_after_scope(interaction, bot, "leland resume", action, ephemeral=True)

    @leland.command(name="evil-mode", description="Toggle upside-down reposts of Leland's messages")
    @app_commands.describe(mode="Turn upside-down reposts on or off")
    @app_commands.choices(mode=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ])
    async def evil_mode_command(interaction: discord.Interaction, mode: str) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _control_permission_ok(interaction, bot):
            return

        async def action() -> str:
            await bot.store.set_evil_mode(mode == "on")
            return f"Evil Leland mode is now **{mode}**."

        await _execute_after_scope(interaction, bot, "leland evil-mode", action, ephemeral=True)

    @leland.command(name="reaction-mode", description="Toggle occasional reactions to Leland's messages")
    @app_commands.describe(mode="Turn occasional emoji reactions on or off")
    @app_commands.choices(mode=[
        app_commands.Choice(name="On", value="on"),
        app_commands.Choice(name="Off", value="off"),
    ])
    async def reaction_mode_command(interaction: discord.Interaction, mode: str) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _control_permission_ok(interaction, bot):
            return

        async def action() -> str:
            await bot.store.set_reaction_mode(mode == "on")
            return f"Reaction mode is now **{mode}**."

        await _execute_after_scope(interaction, bot, "leland reaction-mode", action, ephemeral=True)

    @leland.command(name="delete-data", description="Erase tracked statistics and pause collection")
    async def delete_data_command(interaction: discord.Interaction) -> None:
        if not await _scope_ok(interaction, bot):
            return
        if not await _control_permission_ok(interaction, bot):
            return
        view = DeleteDataConfirmation(bot, interaction.user.id)
        await interaction.response.send_message(
            "This permanently erases tracked statistics and managed local backups, then pauses collection. Continue?",
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

    bot.tree.add_command(leland, guild=guild)

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
        await _send(
            interaction,
            "The tracker could not complete that request. The error was logged.",
            ephemeral=True,
        )

    bot.tree.on_error = on_tree_error


async def _execute_after_scope(
    interaction: discord.Interaction,
    bot: Any,
    label: str,
    action: Callable[[], Awaitable[str]],
    *,
    ephemeral: bool,
) -> None:
    """Run an action after a callback has already applied its scope checks."""
    await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    try:
        content = await action()
    except PermissionError:
        content = "You do not have permission to change the tracker."
    except Exception:
        logger.exception("Slash command %s failed", label)
        content = "The tracker could not complete that request. The error was logged."
    await _send(interaction, content, ephemeral=ephemeral)


def _async_value(value: str) -> Callable[[], Awaitable[str]]:
    async def result() -> str:
        return value

    return result
