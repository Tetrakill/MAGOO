"""Chart geometry for the Ledger's server-rendered SVG charts (v1.27.0).

Pure stdlib: bucket dates and values in, viewBox coordinates out. No
database, no Flask and no colours — every visual choice is a *tone*
name ("accent", "good", "bad", "dim") that base.html maps to a token
through a class, so the page stays inside the Token Purity Rule and
this module is testable without a browser. The dataviz mark specs
apply where the plan allows them: thin bars capped at BAR_MAX units
with a one-unit surface gap between neighbours and between stacked
segments, one shared y-range that always includes 0, round tick
values, and x labels thinned so the last one never collides with its
neighbour. Native <title> tooltips carry the exact figure of every
mark; the tables under the charts are the reader's table view.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

BAR_MAX = 24.0  # a bar never fills its slot; the leftover is air
GAP = 1.0  # surface gap between neighbouring bars and stacked segments
CHAR_W = 6.6  # ~0.6 em of the 11px mono tick font, for label fitting


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    label: str


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    w: float
    h: float
    label: str
    tone: str


@dataclass(frozen=True)
class Series:
    name: str
    tone: str
    kind: str  # "line" | "bar" | "stack"
    points: list[Point]
    path: str
    bars: list[Bar]


@dataclass(frozen=True)
class Chart:
    title: str
    caption: str
    width: int
    height: int
    buckets: int
    empty: bool
    x_ticks: list[tuple[float, str]]
    y_ticks: list[tuple[float, str]]
    zero_y: float
    series: list[Series]
    legend: list[tuple[str, str]]
    plot: tuple[float, float, float, float]  # left, top, right, bottom


def step_days(span_days: int) -> int:
    """Bucket width: daily to a month, weekly to half a year, else 30 days."""
    if span_days <= 31:
        return 1
    if span_days <= 182:
        return 7
    return 30


def bucket_count(since: date, until: date, step: int) -> int:
    """Buckets covering [since, until] inclusive; the last may be short."""
    return max(1, math.ceil(((until - since).days + 1) / step))


def bucket_edges(since: date, until: date, step: int) -> list[date]:
    """Start date of every bucket."""
    n = bucket_count(since, until, step)
    return [since + timedelta(days=i * step) for i in range(n)]


def bucket_of(ts_iso: str, since: date, step: int) -> int:
    """Bucket index of an ESI timestamp ("2026-09-07T12:34:56Z") by its
    UTC date. Range-checking against bucket_count is the caller's job."""
    return (datetime.fromisoformat(ts_iso).date() - since).days // step


def x_labels(edges: Sequence[date], step: int) -> list[str]:
    """Tick text per bucket start: MM-DD up to weekly, YYYY-MM monthly."""
    fmt = "%Y-%m" if step >= 30 else "%m-%d"
    return [d.strftime(fmt) for d in edges]


def nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    """Round tick values covering [lo, hi] at a 1 / 2 / 5 x 10^k step
    (Heckbert's nice numbers). Every step divides 0, so 0 is a tick
    whenever lo <= 0 <= hi."""
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / n
    mag = 10.0 ** math.floor(math.log10(raw))
    frac = raw / mag
    nice = 1.0 if frac < 1.5 else 2.0 if frac < 3.0 else 5.0 if frac < 7.0 else 10.0
    step = nice * mag
    first = math.floor(lo / step + 1e-9)
    last = math.ceil(hi / step - 1e-9)
    return [round(i * step, 10) for i in range(first, last + 1)]


def _r(v: float) -> float:
    return round(v, 1) + 0.0  # + 0.0 turns -0.0 into 0.0


def build_chart(
    title: str,
    x_labels: Sequence[str],
    series: Sequence[tuple[str, str, str, Sequence[float | None]]],
    *,
    fmt: Callable[[float], str],
    caption: str = "",
    width: int = 380,
    height: int = 180,
    pad: tuple[int, int, int, int] = (8, 8, 22, 44),
    x_dates: Sequence[date] | None = None,
) -> Chart:
    """Lay out `series` — (name, tone, kind, values), one value per
    bucket, None for no data — over the `x_labels` buckets. "bar"
    series stand side by side, "stack" series pile up in order, "line"
    series break at None. `pad` is (top, right, bottom, left); the left
    pad grows if the y labels need it. `x_dates` (bucket start dates)
    make the tooltips carry the full ISO date instead of the tick text."""
    top, right, bottom, left = pad
    n = len(x_labels)

    # One shared y-range: stacks contribute their running level so the
    # range and the drawn segments agree; bars and lines their own value.
    levels = [0.0] * n
    lo = hi = 0.0
    seen = False
    for _name, _tone, kind, values in series:
        for i, v in enumerate(values):
            if v is None:
                continue
            seen = seen or v != 0
            if kind == "stack":
                levels[i] += v
                v = levels[i]
            lo, hi = min(lo, v), max(hi, v)
    ticks = nice_ticks(lo, hi)
    ymin, ymax = ticks[0], ticks[-1]
    y_labels = [fmt(t) for t in ticks]
    left = max(left, math.ceil(max(len(s) for s in y_labels) * CHAR_W) + 6)
    x1, y1 = width - right, height - bottom
    slot = (x1 - left) / max(n, 1)

    def y_of(v: float) -> float:
        return top + (ymax - v) / (ymax - ymin) * (y1 - top)

    zero_y = y_of(0.0)
    y_ticks = [(_r(y_of(t)), lbl) for t, lbl in zip(ticks, y_labels)]

    # Every "bar" series takes a column of the slot, the stack one more;
    # bars stay thin (BAR_MAX) and leave the rest of the band as air.
    n_bar = sum(1 for s in series if s[2] == "bar")
    columns = n_bar + (1 if any(s[2] == "stack" for s in series) else 0)
    w = min(BAR_MAX, slot / (columns + 1)) if columns else 0.0
    gap = GAP if w > 3 * GAP else 0.0
    group_x = left + (slot - columns * w) / 2
    stacks = [(k, s[3]) for k, s in enumerate(series) if s[2] == "stack"]

    def x_full(i: int) -> str:
        return x_dates[i].isoformat() if x_dates is not None else x_labels[i]

    def bar(i: int, column: int, y_top: float, y_bot: float, label: str, tone: str) -> Bar:
        bx = group_x + i * slot + column * w + gap / 2
        return Bar(_r(bx), _r(y_top), _r(max(w - gap, 1.0)), _r(y_bot - y_top), label, tone)

    out: list[Series] = []
    levels = [0.0] * n
    column = 0
    for k, (name, tone, kind, values) in enumerate(series):
        points: list[Point] = []
        bars: list[Bar] = []
        path: list[str] = []
        for i, v in enumerate(values):
            if v is None:
                continue
            label = f"{x_full(i)} · {name} {fmt(v)}"
            tone_i = "bad" if v < 0 else tone
            if kind == "line":
                p = Point(_r(left + (i + 0.5) * slot), _r(y_of(v)), label)
                points.append(p)
                path.append(f"{'L' if i and values[i - 1] is not None else 'M'} {p.x} {p.y}")
            elif kind == "bar":
                bars.append(bar(i, column, y_of(max(v, 0.0)), y_of(min(v, 0.0)), label, tone_i))
            else:  # stack: from the running level to level + v
                base, levels[i] = levels[i], levels[i] + v
                y_top, y_bot = y_of(max(base, levels[i])), y_of(min(base, levels[i]))
                # the surface gap sits under the next segment, so the
                # stack's top edge stays exact
                trim = gap if any(vals[i] for j, vals in stacks if j > k) else 0.0
                bars.append(bar(i, n_bar, y_top + trim, y_bot, label, tone_i))
        if kind == "bar":
            column += 1
        out.append(Series(name, tone, kind, points, " ".join(path), bars))

    # x ticks: every ceil(n/6)-th bucket plus the last; the last wins when
    # it would land on top of the previous one. Labels are nudged inside
    # the viewBox so the edge ones are never clipped.
    every = math.ceil(n / 6) if n else 1
    idx = list(range(0, n, every))
    if n and idx[-1] != n - 1:
        if n - 1 - idx[-1] < every / 2:
            idx.pop()
        idx.append(n - 1)
    x_ticks = []
    for i in idx:
        half = len(x_labels[i]) * CHAR_W / 2
        cx = min(max(left + (i + 0.5) * slot, half), width - half)
        x_ticks.append((_r(cx), x_labels[i]))

    return Chart(
        title=title,
        caption=caption,
        width=width,
        height=height,
        buckets=n,
        empty=not seen,
        x_ticks=x_ticks,
        y_ticks=y_ticks,
        zero_y=_r(zero_y),
        series=out,
        legend=[(s[0], s[1]) for s in series],
        plot=(_r(left), _r(top), _r(x1), _r(y1)),
    )
