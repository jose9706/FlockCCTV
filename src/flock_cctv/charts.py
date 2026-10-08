"""Draw the report charts as dark PNG cards.

Each chart function takes plain, already-computed values and returns PNG
bytes; the commands module reads the data and writes the text replies. All
layout is in logical units on a ``WIDTH``-wide card and drawn ``_DRAW`` times
larger, then downscaled once so edges and text are antialiased. The output is
``OUTPUT_SCALE`` times the logical size so it stays sharp on high-density
screens.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .text import duration as _duration

WIDTH = 600
MARGIN = 22
OUTPUT_SCALE = 2
_SUPERSAMPLE = 2
_DRAW = OUTPUT_SCALE * _SUPERSAMPLE

# Type sizes. Discord previews attachments around 400px wide, so axis text is
# kept to at least 3% of the card width (about 12px there).
EYEBROW_SIZE = 13
TITLE_SIZE = 26
KPI_SIZE = 26
LABEL_SIZE = 16
HEADING_SIZE = 19
AXIS_SIZE = 18
NOTE_SIZE = 16

# Dark chart chrome and the categorical palette stepped for a dark surface.
# The slot order is validated for colour-vision deficiency on adjacent pairs.
SURFACE = "#1a1a19"
INK = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"
GRID = "#2c2c2a"
BASELINE = "#383835"
BAND = "#21211f"
SERIES = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
ALONE = "#6b6a65"
OTHERS = "#4a4945"
UNRANKED = "#9b9890"
GOOD = "#0ca30c"
CRITICAL = "#d03b3b"
HATCH = "#3a3936"
NIGHT_BAND = "#202733"
NIGHT_INK = "#86b6ef"
# Sequential blue, dark (few) to light (many) on the dark surface.
BLUE_RAMP = (
    "#104281", "#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5",
    "#5598e7", "#6da7ec", "#86b6ef", "#9ec5f4", "#b7d3f6", "#cde2fb",
)
# Ordered bins (burst sizes): light for small, dark for large.
ORDINAL_BLUE = ("#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#104281")
EMPTY_CELL = "#232322"
TILE = "#222221"

_Y_GUTTER = 54
_AVG_GUTTER = 84
_BAR_MAX = 26
_RADIUS = 4

_FONT_FILES = {False: "DejaVuSans.ttf", True: "DejaVuSans-Bold.ttf"}
_fonts: dict[tuple[int, bool], Any] = {}


def _font(size: int, bold: bool = False) -> Any:
    """Return a cached font ``size`` logical units tall, at drawing scale."""
    key = (size, bold)
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(_FONT_FILES[bold], size * _DRAW)
        except OSError:
            try:
                _fonts[key] = ImageFont.truetype(_FONT_FILES[False], size * _DRAW)
            except OSError:
                _fonts[key] = ImageFont.load_default(size=size * _DRAW)
    return _fonts[key]


def drawable(text: str) -> str:
    """Drop emoji and joiners the bundled font cannot draw, so names never show boxes."""
    kept = []
    removed = False
    for char in text:
        point = ord(char)
        if point >= 0x1F000 or point in (0xFE0F, 0xFE0E, 0x200D, 0x20E3) or 0xE0000 <= point <= 0xE007F:
            removed = True
            continue
        kept.append(char)
    result = "".join(kept)
    if removed:
        # "Named 🎮's" would otherwise become "Named 's".
        result = re.sub(r"\s+(?=[',.!?:;])", "", result)
    return " ".join(result.split())


def _mix(colour: str, other: str, amount: float) -> str:
    a = [int(colour[index:index + 2], 16) for index in (1, 3, 5)]
    b = [int(other[index:index + 2], 16) for index in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def muted(colour: str) -> str:
    """The de-emphasised step of a series colour."""
    return _mix(colour, SURFACE, 0.55)


class _Card:
    """A tall dark canvas drawn in logical units, cropped to the used height on save."""

    def __init__(self, height: int = 1400) -> None:
        self.image = Image.new("RGB", (WIDTH * _DRAW, height * _DRAW), SURFACE)
        self.draw = ImageDraw.Draw(self.image)

    def width(self, text: str, size: int, bold: bool = False) -> float:
        return _font(size, bold).getlength(text) / _DRAW

    def fit(self, text: str, size: int, room: float, bold: bool = False) -> str:
        """Shorten ``text`` with an ellipsis until it fits in ``room``."""
        if self.width(text, size, bold) <= room:
            return text
        while text and self.width(text + "…", size, bold) > room:
            text = text[:-1]
        return text.rstrip() + "…"

    def text(
        self, xy: tuple[float, float], text: str, size: int, fill: str = INK,
        *, bold: bool = False, anchor: str = "la",
    ) -> None:
        self.draw.text((xy[0] * _DRAW, xy[1] * _DRAW), text, font=_font(size, bold), fill=fill, anchor=anchor)

    def rect(
        self, box: Sequence[float], fill: str, *, radius: float = 0,
        corners: tuple[bool, bool, bool, bool] | None = None,
    ) -> None:
        x0, y0, x1, y1 = (value * _DRAW for value in box)
        if x1 - x0 < 1 or y1 - y0 < 1:
            return
        radius = min(radius * _DRAW, (x1 - x0) / 2, (y1 - y0) / 2)
        if radius >= 1:
            self.draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=fill, corners=corners)
        else:
            self.draw.rectangle((x0, y0, x1, y1), fill=fill)

    def column(self, centre: float, width: float, top: float, base: float, fill: str) -> None:
        """A column with a rounded data end and a square baseline end."""
        self.rect(
            (centre - width / 2, top, centre + width / 2, base), fill,
            radius=_RADIUS, corners=(True, True, False, False),
        )

    def line(self, points: Sequence[tuple[float, float]], fill: str, width: float = 1) -> None:
        self.draw.line(
            [(x * _DRAW, y * _DRAW) for x, y in points], fill=fill,
            width=max(1, round(width * _DRAW)), joint="curve",
        )

    def dot(self, x: float, y: float, radius: float, fill: str) -> None:
        ring = radius + 2
        self.draw.ellipse(((x - ring) * _DRAW, (y - ring) * _DRAW, (x + ring) * _DRAW, (y + ring) * _DRAW), fill=SURFACE)
        self.draw.ellipse(((x - radius) * _DRAW, (y - radius) * _DRAW, (x + radius) * _DRAW, (y + radius) * _DRAW), fill=fill)

    def ring(self, x: float, y: float, radius: float, outline: str) -> None:
        self.draw.ellipse(
            ((x - radius) * _DRAW, (y - radius) * _DRAW, (x + radius) * _DRAW, (y + radius) * _DRAW),
            outline=outline, width=2 * _DRAW,
        )

    def hatch(self, box: Sequence[float], colour: str, spacing: float = 5) -> None:
        """45° lines clipped to ``box``: the texture for time the bot was not watching."""
        x0, y0, x1, y1 = box
        width, height = round((x1 - x0) * _DRAW), round((y1 - y0) * _DRAW)
        if width < 1 or height < 1:
            return
        mask = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(mask)
        step = max(1, round(spacing * _DRAW))
        for offset in range(-height, width + 1, step):
            draw.line((offset, height, offset + height, 0), fill=255, width=_DRAW)
        self.image.paste(colour, (round(x0 * _DRAW), round(y0 * _DRAW)), mask)

    def avatar(self, x: float, y: float, size: float, data: bytes | None, fill: str, name: str) -> None:
        """A round avatar at ``(x, y)`` (top left), or the name's initial on ``fill``."""
        pixels = round(size * _DRAW)
        mask = Image.new("L", (pixels, pixels), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, pixels - 1, pixels - 1), fill=255)
        picture = None
        if data:
            try:
                with Image.open(BytesIO(data)) as source:
                    picture = source.convert("RGB").resize((pixels, pixels), Image.LANCZOS)
            except Exception:  # A broken download falls back to the initial.
                picture = None
        if picture is None:
            picture = Image.new("RGB", (pixels, pixels), fill)
            initial = next((char for char in drawable(name) if char.isalnum()), "")
            if initial:
                ImageDraw.Draw(picture).text(
                    (pixels / 2, pixels / 2), initial.upper(),
                    font=_font(round(size * 0.5), True), fill=INK, anchor="mm",
                )
        self.image.paste(picture, (round(x * _DRAW), round(y * _DRAW)), mask)

    def png(self, height: float) -> bytes:
        height = math.ceil(height)
        full = self.image.crop((0, 0, WIDTH * _DRAW, height * _DRAW))
        size = (WIDTH * OUTPUT_SCALE, height * OUTPUT_SCALE)
        card = full.resize(size, Image.LANCZOS).convert("RGBA")
        mask = Image.new("L", (WIDTH * _DRAW, height * _DRAW), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, WIDTH * _DRAW - 1, height * _DRAW - 1), radius=14 * _DRAW, fill=255,
        )
        card.putalpha(mask.resize(size, Image.LANCZOS))
        output = BytesIO()
        card.save(output, format="PNG", optimize=True)
        return output.getvalue()


# ---------- shared pieces ----------

def _header(card: _Card, eyebrow: str, title: str, stats: Sequence[tuple[str, str]] = ()) -> float:
    """Eyebrow (report and period), title, and a strip of ``(value, label)`` figures."""
    room = WIDTH - 2 * MARGIN
    card.text((MARGIN, 20), card.fit(drawable(eyebrow).upper(), EYEBROW_SIZE, room, bold=True), EYEBROW_SIZE, INK_MUTED, bold=True)
    size = TITLE_SIZE
    title = drawable(title)
    while size > 18 and card.width(title, size, bold=True) > room:
        size -= 1
    card.text((MARGIN, 42), card.fit(title, size, room, bold=True), size, INK, bold=True)
    y = 92.0
    if stats:
        column = room / len(stats)
        for index, (value, label) in enumerate(stats):
            x = MARGIN + index * column
            value_size = KPI_SIZE
            value = drawable(value)
            while value_size > 16 and card.width(value, value_size, bold=True) > column - 14:
                value_size -= 1
            card.text((x, y), card.fit(value, value_size, column - 14, bold=True), value_size, INK, bold=True)
            card.text((x, y + 36), card.fit(label, LABEL_SIZE, column - 12), LABEL_SIZE, INK_SECONDARY)
        y += 74
    return y + 14


@dataclass(frozen=True)
class Key:
    """One footer key entry: ``kind`` is swatch, hatch, ring, line, or text."""

    kind: str
    label: str
    colour: str = INK_SECONDARY


def _footer(card: _Card, y: float, keys: Sequence[Key], notes: Sequence[str] = ()) -> float:
    """A hairline, then the key wrapped onto lines, then muted notes."""
    card.line([(MARGIN, y), (WIDTH - MARGIN, y)], GRID)
    y += 14
    x = float(MARGIN)
    for key in keys:
        mark = 0 if key.kind == "text" else 20
        needed = mark + card.width(key.label, NOTE_SIZE)
        if x > MARGIN and x + needed > WIDTH - MARGIN:
            x, y = float(MARGIN), y + 26
        if key.kind == "swatch":
            card.rect((x, y + 3, x + 13, y + 16), key.colour, radius=3)
        elif key.kind == "hatch":
            card.rect((x, y + 3, x + 13, y + 16), EMPTY_CELL)
            card.hatch((x, y + 3, x + 13, y + 16), "#5c5b57", spacing=4)
        elif key.kind == "ring":
            card.ring(x + 6.5, y + 9.5, 5, INK_MUTED)
        elif key.kind == "line":
            card.line([(x, y + 9.5), (x + 14, y + 9.5)], key.colour, 2)
        card.text((x + mark, y), key.label, NOTE_SIZE, INK_SECONDARY)
        x += needed + 18
    y += 30 if keys else 0
    for note in notes:
        for line in _wrap(card, note, NOTE_SIZE, WIDTH - 2 * MARGIN):
            card.text((MARGIN, y), line, NOTE_SIZE, INK_MUTED)
            y += 22
    return y + 10


def _wrap(card: _Card, text: str, size: int, room: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and card.width(candidate, size) > room:
            lines.append(current)
            current = word
        else:
            current = candidate
    return lines + ([current] if current else [])


_DURATION_STEPS = (
    60, 300, 600, 900, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400, 172800, 604800,
)


def axis_ticks(peak: float, kind: str) -> list[float]:
    """Return round ticks from zero covering ``peak`` with the least headroom.

    ``kind`` is ``count`` (whole numbers), ``average`` (fractions allowed), or
    ``duration`` (seconds, in round clock steps). Two to five intervals.
    """
    if peak <= 0:
        return [0.0, 3600.0 if kind == "duration" else 1.0]
    if kind == "duration":
        steps = [float(step) for step in _DURATION_STEPS]
        steps.append(math.ceil(peak / 4 / 604800) * 604800.0)
    else:
        magnitude = 10 ** math.floor(math.log10(peak / 4))
        steps = [multiple * magnitude for multiple in (1, 2, 2.5, 5, 10, 20, 25, 50)]
        if kind == "count":
            steps = [float(max(1, math.ceil(step))) for step in steps]
    fitting = [step for step in steps if 2 <= math.ceil(peak / step - 1e-9) <= 5]
    if not fitting:
        fitting = [step for step in steps if math.ceil(peak / step - 1e-9) <= 5] or [steps[-1]]
    step = min(fitting, key=lambda value: (math.ceil(peak / value - 1e-9) * value - peak, -value))
    intervals = max(1, math.ceil(peak / step - 1e-9))
    return [index * step for index in range(intervals + 1)]


def axis_label(value: float, kind: str) -> str:
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


def value_label(value: float, kind: str) -> str:
    if kind == "duration":
        return _duration(value)
    if kind == "average":
        return f"{value:.1f}"
    return f"{int(round(value)):,}"


@dataclass(frozen=True)
class Panel:
    """One column chart: a heading, values, a colour, and the value kind.

    ``highlight`` keeps one column in full colour and mutes the rest.
    ``average`` draws a labelled reference line. ``label_all`` prints every value.
    """

    heading: str
    values: Sequence[float]
    colour: str
    kind: str
    average: float | None = None
    average_label: str | None = None
    highlight: int | None = None
    label_all: bool = False


def _column_panel(
    card: _Card, top: float, height: float, panel: Panel, *, states: Sequence[str | None] | None = None,
    bands: Sequence[tuple[int, int]] = (), right_gutter: float = 0,
) -> tuple[float, float, float, float]:
    """Draw a heading and columns; return ``(left, right, slot, base)`` for x labels.

    ``states[i]`` is ``unwatched`` (hatched), ``ghost`` (a ring at the
    baseline), ``partial`` (a lighter column), or ``None``.
    """
    left, right = MARGIN + _Y_GUTTER, WIDTH - MARGIN - right_gutter
    card.text((MARGIN, top), panel.heading, HEADING_SIZE, INK, bold=True)
    values = list(panel.values)
    plot_top = top + 46
    base = top + height
    ticks = axis_ticks(max(values, default=0.0), panel.kind)
    scale = (base - plot_top) / ticks[-1]
    slot = (right - left) / max(1, len(values))
    for start, end in bands:
        card.rect((left + start * slot, plot_top - 8, left + end * slot, base), BAND)
    for tick in ticks:
        y = base - tick * scale
        card.line([(left, y), (right, y)], BASELINE if tick == 0 else GRID)
        card.text((left - 8, y), axis_label(tick, panel.kind), AXIS_SIZE, INK_MUTED, anchor="rm")
    bar = min(_BAR_MAX, slot * 0.62)
    for index, value in enumerate(values):
        centre = left + (index + 0.5) * slot
        state = states[index] if states else None
        if state == "unwatched" and value <= 0:
            card.hatch((centre - bar / 2, plot_top, centre + bar / 2, base), HATCH)
            continue
        if state == "ghost":
            card.ring(centre, base - 7, min(5, bar / 2 + 1), INK_MUTED)
            continue
        if value <= 0:
            continue
        colour = panel.colour
        if panel.highlight is not None and index != panel.highlight:
            colour = muted(colour)
        if state == "partial":
            colour = _mix(colour, SURFACE, 0.5)
        column_top = base - max(2.0, value * scale)
        card.column(centre, bar, column_top, base, colour)
        if panel.label_all:
            card.text((centre, column_top - 5), value_label(value, panel.kind), AXIS_SIZE - 2, INK_SECONDARY, anchor="mb")
    if panel.average is not None and panel.average > 0:
        y = base - panel.average * scale
        card.line([(left, y), (right, y)], INK_SECONDARY, 1.5)
        label = panel.average_label or f"avg {axis_label(panel.average, panel.kind)}"
        size = NOTE_SIZE - 1
        card.text((right + 8, y), card.fit(label, size, right_gutter - 8, bold=True), size, INK_SECONDARY, bold=True, anchor="lm")
    peak = max(values, default=0.0)
    if peak > 0 and not panel.label_all:
        index = values.index(peak)
        text = value_label(peak, panel.kind)
        half = card.width(text, AXIS_SIZE - 1, bold=True) / 2
        centre = min(max(left + (index + 0.5) * slot, left + half), right - half)
        card.text((centre, base - peak * scale - 5), text, AXIS_SIZE - 1, INK, bold=True, anchor="mb")
    return left, right, slot, base


def _x_labels(
    card: _Card, left: float, slot: float, base: float, labels: Sequence[str],
    positions: Sequence[int] | None = None, sublabels: Sequence[str] | None = None,
) -> float:
    """Label columns below ``base``; thin them so neighbours never overlap."""
    if positions is None:
        widest = max((card.width(label, AXIS_SIZE) for label in labels), default=0)
        step = max(1, math.ceil((widest + 10) / slot))
        positions = range(0, len(labels), step)
    for index in positions:
        x = left + (index + 0.5) * slot
        card.text((x, base + 8), labels[index], AXIS_SIZE, INK_SECONDARY, anchor="mt")
        if sublabels:
            card.text((x, base + 32), sublabels[index], NOTE_SIZE - 2, INK_MUTED, anchor="mt")
    return base + (56 if sublabels else 36)


# ---------- charts ----------

def daily_chart(
    eyebrow: str, title: str, stats: Sequence[tuple[str, str]], labels: Sequence[str],
    label_positions: Sequence[int] | None, messages: Panel, voice: Panel,
    states: Sequence[str | None], bands: Sequence[tuple[int, int]], keys: Sequence[Key],
    notes: Sequence[str] = (),
) -> bytes:
    """Messages and voice per day (or week or month), sharing one x axis."""
    card = _Card()
    y = _header(card, eyebrow, title, stats)
    _column_panel(card, y, 176, messages, states=states, bands=bands, right_gutter=_AVG_GUTTER)
    y += 196
    left, _, slot, base = _column_panel(card, y, 166, voice, states=states, bands=bands, right_gutter=_AVG_GUTTER)
    y = _x_labels(card, left, slot, base, labels, label_positions)
    return card.png(_footer(card, y + 4, keys, notes))


def weekday_chart(
    eyebrow: str, title: str, stats: Sequence[tuple[str, str]], messages: Panel, voice: Panel,
    labels: Sequence[str], sublabels: Sequence[str], keys: Sequence[Key], notes: Sequence[str] = (),
) -> bytes:
    """Average messages and voice per weekday, with the top day emphasised."""
    card = _Card()
    y = _header(card, eyebrow, title, stats)
    _column_panel(card, y, 170, messages, right_gutter=_AVG_GUTTER)
    y += 190
    left, _, slot, base = _column_panel(card, y, 160, voice, right_gutter=_AVG_GUTTER)
    y = _x_labels(card, left, slot, base, labels, range(len(labels)), sublabels)
    return card.png(_footer(card, y + 4, keys, notes))


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def hours_chart(
    eyebrow: str, title: str, stats: Sequence[tuple[str, str]],
    grid: Sequence[Sequence[float]], voice: Panel | None, night_hours: int, notes: Sequence[str] = (),
) -> bytes:
    """A weekday × hour heatmap of messages above voice time by hour, aligned by hour.

    With no voice time, ``voice`` is ``None`` and the hour labels go under the heatmap.
    """
    card = _Card()
    y = _header(card, eyebrow, title, stats)
    left, right = MARGIN + _Y_GUTTER, WIDTH - MARGIN
    slot = (right - left) / 24
    card.text((MARGIN, y), "Messages, by weekday and hour", HEADING_SIZE, INK, bold=True)
    top = y + 52
    cell = 24
    peak = max((max(row, default=0) for row in grid), default=0)
    card.rect((left - 2, top - 6, left + night_hours * slot + 2, top + 7 * cell + 2), NIGHT_BAND)
    card.text((left + 2, top - 10), "night owl", NOTE_SIZE - 2, NIGHT_INK, bold=True, anchor="lb")
    for row_index, row in enumerate(grid):
        card.text((left - 8, top + row_index * cell + cell / 2), _WEEKDAYS[row_index], AXIS_SIZE, INK_SECONDARY, anchor="rm")
        for hour, value in enumerate(row):
            x = left + hour * slot
            box = (x + 1.5, top + row_index * cell + 1.5, x + slot - 1.5, top + (row_index + 1) * cell - 1.5)
            if value <= 0 or peak <= 0:
                card.rect(box, EMPTY_CELL, radius=3)
            else:
                # Square-root scale so a few busy hours don't wash out the rest.
                step = math.sqrt(value / peak)
                card.rect(box, BLUE_RAMP[min(len(BLUE_RAMP) - 1, round(step * (len(BLUE_RAMP) - 1)))], radius=3)
    y = top + 7 * cell + 10
    swatches = 6
    scale_left = right - swatches * 17 - card.width("more", NOTE_SIZE - 2) - 6
    card.text((scale_left - 6, y + 7), "fewer", NOTE_SIZE - 2, INK_MUTED, anchor="rm")
    for index in range(swatches):
        colour = BLUE_RAMP[round(index / (swatches - 1) * (len(BLUE_RAMP) - 1))]
        card.rect((scale_left + index * 17, y, scale_left + index * 17 + 14, y + 14), colour, radius=2)
    card.text((scale_left + swatches * 17 + 4, y + 7), "more", NOTE_SIZE - 2, INK_MUTED, anchor="lm")
    hours = [f"{hour:02d}" for hour in range(24)]
    if voice is None:
        y = _x_labels(card, left, slot, y - 4, hours, range(0, 24, 3))
    else:
        y += 30
        left, _, slot, base = _column_panel(card, y, 150, voice, bands=[(0, night_hours)])
        y = _x_labels(card, left, slot, base, hours, range(0, 24, 3))
    keys = [Key("swatch", f"00:00–{night_hours:02d}:00, the night-owl window", NIGHT_BAND), Key("swatch", "no messages", EMPTY_CELL)]
    return card.png(_footer(card, y + 4, keys, notes))


@dataclass(frozen=True)
class Tile:
    label: str
    value: str
    change: str  # e.g. "▲ +31%  vs 412"


@dataclass(frozen=True)
class PacePanel:
    """Running totals over elapsed time: ``(fraction, value)`` points for each window.

    The labels are short values printed at each line's end; the key names the lines.
    """

    heading: str
    kind: str
    colour: str
    previous: Sequence[tuple[float, float]]
    current: Sequence[tuple[float, float]]
    previous_label: str
    current_label: str


def compare_chart(
    eyebrow: str, title: str, tiles: Sequence[Tile], panels: Sequence[PacePanel],
    x_ticks: Sequence[tuple[float, str]], keys: Sequence[Key], notes: Sequence[str] = (),
) -> bytes:
    """Stat tiles, then running-total lines for this period against the last."""
    card = _Card()
    y = _header(card, eyebrow, title)
    gap = 10
    tile_width = (WIDTH - 2 * MARGIN - gap * (len(tiles) - 1)) / max(1, len(tiles))
    for index, tile in enumerate(tiles):
        x = MARGIN + index * (tile_width + gap)
        card.rect((x, y, x + tile_width, y + 104), TILE, radius=10)
        card.text((x + 14, y + 12), card.fit(tile.label, LABEL_SIZE, tile_width - 24), LABEL_SIZE, INK_SECONDARY)
        size = KPI_SIZE
        while size > 16 and card.width(tile.value, size, bold=True) > tile_width - 24:
            size -= 1
        card.text((x + 14, y + 36), tile.value, size, INK, bold=True)
        card.text((x + 14, y + 76), card.fit(tile.change, NOTE_SIZE - 1, tile_width - 24), NOTE_SIZE - 1, INK_SECONDARY)
    y += 130
    for panel in panels:
        card.text((MARGIN, y), panel.heading, HEADING_SIZE, INK, bold=True)
        plot_top, base = y + 44, y + 44 + 130
        left, right = MARGIN + _Y_GUTTER, WIDTH - MARGIN - 84
        peak = max([value for _, value in (*panel.previous, *panel.current)], default=0.0)
        ticks = axis_ticks(peak, panel.kind)
        scale = (base - plot_top) / ticks[-1]
        for tick in ticks:
            tick_y = base - tick * scale
            card.line([(left, tick_y), (right, tick_y)], BASELINE if tick == 0 else GRID)
            card.text((left - 8, tick_y), axis_label(tick, panel.kind), AXIS_SIZE, INK_MUTED, anchor="rm")

        def point(fraction: float, value: float) -> tuple[float, float]:
            return left + fraction * (right - left), base - value * scale

        previous = [point(*item) for item in panel.previous]
        current = [point(*item) for item in panel.current]
        if len(previous) > 1:
            card.line(previous, ALONE, 2)
        if len(current) > 1:
            card.line(current, panel.colour, 2.5)
        ends = []
        if previous:
            card.dot(*previous[-1], 4, ALONE)
            ends.append([previous[-1][1], panel.previous_label, INK_MUTED, False])
        if current:
            card.dot(*current[-1], 5, panel.colour)
            ends.append([current[-1][1], panel.current_label, INK, True])
        # Keep the two end labels apart; the lower one moves down.
        if len(ends) == 2 and abs(ends[0][0] - ends[1][0]) < 20:
            lower, upper = sorted(ends, key=lambda end: -end[0])
            middle = (lower[0] + upper[0]) / 2
            lower[0], upper[0] = middle + 10, middle - 10
        for end_y, label, colour, bold in ends:
            card.text((right + 10, end_y), card.fit(label, NOTE_SIZE, 74, bold), NOTE_SIZE, colour, bold=bold, anchor="lm")
        shown_right = -1e9
        for fraction, label in x_ticks:
            x = left + fraction * (right - left)
            half = card.width(label, AXIS_SIZE - 2) / 2
            if x - half < shown_right + 8 or x + half > right + 40:
                continue
            card.line([(x, base), (x, base + 4)], BASELINE)
            card.text((x, base + 8), label, AXIS_SIZE - 2, INK_SECONDARY, anchor="mt")
            shown_right = x + half
        y = base + 44
    return card.png(_footer(card, y, keys, notes))


def bursts_chart(
    eyebrow: str, title: str, stats: Sequence[tuple[str, str]], labels: Sequence[str],
    bursts: Sequence[float], messages: Sequence[float], notes: Sequence[str] = (),
) -> bytes:
    """Burst counts per size, then one bar of where the messages went by burst size."""
    card = _Card()
    y = _header(card, eyebrow, title, stats)
    panel = Panel("How many bursts of each size", bursts, ORDINAL_BLUE[2], "count", label_all=True)
    left, _, slot, base = _column_panel(card, y, 180, panel)
    y = _x_labels(card, left, slot, base, labels, range(len(labels)))
    total = sum(messages)
    card.text((MARGIN, y + 4), f"Where the {int(total):,} messages went", HEADING_SIZE, INK, bold=True)
    y += 40
    x = float(MARGIN)
    span = WIDTH - 2 * MARGIN
    filled = [index for index, value in enumerate(messages) if value > 0]
    for index in filled:
        width = span * messages[index] / total
        first, last = index == filled[0], index == filled[-1]
        card.rect(
            (x, y, x + width - (0 if last else 2), y + 34), ORDINAL_BLUE[index],
            radius=6 if first or last else 0, corners=(first, last, last, first),
        )
        label = f"{labels[index]} · {messages[index] / total:.0%}"
        if card.width(label, NOTE_SIZE, bold=True) + 14 < width:
            ink = SURFACE if index < 2 else INK
            card.text((x + 8, y + 17), label, NOTE_SIZE, ink, bold=True, anchor="lm")
        x += width
    y += 50
    keys = [Key("swatch", "single messages", ORDINAL_BLUE[0]), Key("swatch", "bigger bursts", ORDINAL_BLUE[4])]
    return card.png(_footer(card, y, keys, notes))


@dataclass(frozen=True)
class CompanyRow:
    """A ranked row: ``avatar`` is image bytes, or ``None`` for an initial (or no mark for groups)."""

    name: str
    seconds: float
    colour: str
    detail: str  # muted text after the duration, e.g. "36%"
    avatar: bytes | None = None
    group: bool = False  # Alone or Others: no avatar mark, placed under a divider


def company_chart(
    eyebrow: str, title: str, stats: Sequence[tuple[str, str]], rows: Sequence[CompanyRow],
    keys: Sequence[Key], notes: Sequence[str] = (),
) -> bytes:
    """Ranked horizontal bars of shared voice time; groups sit under a divider."""
    card = _Card()
    y = _header(card, eyebrow, title, stats)
    name_left, bar_left = MARGIN + 32, MARGIN + 182
    value_room = 118
    bar_room = WIDTH - MARGIN - value_room - bar_left
    peak = max((row.seconds for row in rows), default=0.0) or 1.0
    divided = False
    for row in rows:
        if row.group and not divided and y > 0:
            divided = True
            if any(not other.group for other in rows):
                card.line([(MARGIN, y + 4), (WIDTH - MARGIN, y + 4)], GRID)
                y += 14
        centre = y + 15
        if not row.group:
            card.avatar(MARGIN, centre - 12, 24, row.avatar, row.colour, row.name)
        name = card.fit(drawable(row.name) or "Someone", LABEL_SIZE + 1, bar_left - name_left - 10)
        card.text((name_left, centre), name, LABEL_SIZE + 1, INK_SECONDARY if row.group else INK, anchor="lm")
        width = max(3.0, bar_room * row.seconds / peak)
        card.rect((bar_left, centre - 11, bar_left + width, centre + 11), row.colour, radius=_RADIUS, corners=(False, True, True, False))
        value = _duration(row.seconds)
        x = bar_left + width + 8
        card.text((x, centre), value, LABEL_SIZE, INK, bold=True, anchor="lm")
        card.text((x + card.width(value, LABEL_SIZE, bold=True) + 7, centre), row.detail, LABEL_SIZE, INK_MUTED, anchor="lm")
        y += 36
    return card.png(_footer(card, y + 10, keys, notes))


@dataclass(frozen=True)
class TrendRow:
    name: str
    values: Sequence[float]
    colour: str
    total: str
    avatar: bytes | None = None
    group: bool = False


def company_trend_chart(
    eyebrow: str, title: str, stats: Sequence[tuple[str, str]], rows: Sequence[TrendRow],
    labels: Sequence[str], label_positions: Sequence[int] | None, keys: Sequence[Key],
    notes: Sequence[str] = (),
) -> bytes:
    """One strip of columns per companion, all on one shared scale."""
    card = _Card()
    y = _header(card, eyebrow, title, stats)
    name_left, left, right = MARGIN + 30, MARGIN + 160, WIDTH - MARGIN - 70
    count = max((len(row.values) for row in rows), default=1)
    slot = (right - left) / max(1, count)
    peak = max((max(row.values, default=0) for row in rows), default=0.0) or 1.0
    row_height = 50
    bar = min(_BAR_MAX, slot * 0.64)
    for row in rows:
        base = y + row_height - 6
        card.line([(left, base), (right, base)], BASELINE)
        if not row.group:
            card.avatar(MARGIN, base - 22, 22, row.avatar, row.colour, row.name)
        name = card.fit(drawable(row.name) or "Someone", LABEL_SIZE, left - name_left - 10)
        card.text((name_left, base - 11), name, LABEL_SIZE, INK_SECONDARY if row.group else INK, anchor="lm")
        for index, value in enumerate(row.values):
            if value > 0:
                centre = left + (index + 0.5) * slot
                card.column(centre, bar, base - max(2.0, (row_height - 14) * value / peak), base, row.colour)
        card.text((right + 8, base - 11), row.total, LABEL_SIZE - 1, INK_SECONDARY, bold=True, anchor="lm")
        y += row_height
    y = _x_labels(card, left, slot, y - 6, labels, label_positions)
    return card.png(_footer(card, y + 4, keys, notes))


@dataclass(frozen=True)
class UptimeDay:
    """One local day: ``spans`` are ``(start, end, kind)`` as fractions of the day."""

    label: str
    spans: Sequence[tuple[float, float, str]]
    percent: float


_SPAN_COLOURS = {"observed": GOOD, "outage": CRITICAL, "idle": ALONE}


def uptime_chart(
    eyebrow: str, title: str, stats: Sequence[tuple[str, str]], days: Sequence[UptimeDay],
    notes: Sequence[str] = (),
) -> bytes:
    """A 24-hour timeline per day with outages and pauses where they happened."""
    card = _Card(height=200 + 40 * 26 + 400)
    y = _header(card, eyebrow, title, stats)
    left, right = MARGIN + 66, WIDTH - MARGIN - 62
    row = 24
    for hour in range(0, 25, 6):
        x = left + (right - left) * hour / 24
        card.text((x, y), f"{hour:02d}:00", NOTE_SIZE - 2, INK_MUTED, anchor="mt")
        card.line([(x, y + 22), (x, y + 24 + row * len(days))], GRID)
    y += 26
    for day in days:
        card.text((left - 8, y + row / 2), day.label, AXIS_SIZE - 2, INK_SECONDARY, anchor="rm")
        card.rect((left, y + 5, right, y + row - 5), EMPTY_CELL, radius=3)
        for start, end, kind in day.spans:
            card.rect(
                (left + (right - left) * start, y + 5, left + (right - left) * end, y + row - 5),
                _SPAN_COLOURS.get(kind, ALONE),
            )
        text = "100%" if day.percent >= 99.95 else f"{day.percent:.1f}%"
        low = day.percent < 99
        card.text((right + 8, y + row / 2), text, AXIS_SIZE - 2, INK if low else INK_MUTED, bold=low, anchor="lm")
        y += row
    keys = [Key("swatch", "watching", GOOD), Key("swatch", "outage", CRITICAL), Key("swatch", "paused", ALONE)]
    return card.png(_footer(card, y + 14, keys, notes))
