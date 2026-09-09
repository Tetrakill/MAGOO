"""v1.27.0 Ledger charts: the pure geometry in magoo/charts.py (bucket
rule, tick generation, bar / stack / line layout, tooltip text) and the
_chart.html macro rendered through the app's Jinja environment — the
partial must draw from tone classes alone (no colour literal, no inline
style inside the <svg>) and show the empty sentence instead of an svg
when there is nothing to plot. No database is touched."""

import math
from datetime import date, timedelta

from flask import render_template_string

from magoo import charts

from conftest import template_app


def isk(v):
    return f"{v:,.0f}"


def window(days):
    until = date(2026, 9, 7)
    return until - timedelta(days=days - 1), until


# --- buckets ---------------------------------------------------------------


def test_step_rule_and_bucket_counts():
    for days, step, buckets in ((7, 1, 7), (30, 1, 30), (90, 7, 13)):
        since, until = window(days)
        assert charts.step_days(days) == step
        assert charts.bucket_count(since, until, step) == buckets
        edges = charts.bucket_edges(since, until, step)
        assert len(edges) == buckets
        assert edges[0] == since and edges[-1] <= until
        assert edges[1] - edges[0] == timedelta(days=step)
    # the `all` window of a three-year-old ledger: monthly, never a crowd
    since, until = window(3 * 365 + 1)
    step = charts.step_days((until - since).days + 1)
    assert step == 30
    assert charts.bucket_count(since, until, step) <= 37


def test_bucket_of_edges():
    since, until = window(7)
    assert charts.bucket_of(f"{since.isoformat()}T00:00:01Z", since, 1) == 0
    last = charts.bucket_count(since, until, 1) - 1
    assert charts.bucket_of(f"{until.isoformat()}T23:55:00Z", since, 1) == last
    # a 7-day step flips exactly at midnight UTC of the eighth day
    since = date(2026, 6, 1)
    assert charts.bucket_of("2026-06-07T23:59:59Z", since, 7) == 0
    assert charts.bucket_of("2026-06-08T00:00:00Z", since, 7) == 1
    assert charts.bucket_of("2026-06-08T00:00:00+00:00", since, 7) == 1


def test_x_labels_format_and_thinning():
    since, until = window(30)
    edges = charts.bucket_edges(since, until, 1)
    labels = charts.x_labels(edges, 1)
    assert labels[0] == since.strftime("%m-%d") and len(labels) == 30
    assert charts.x_labels(edges[:2], 7) == [since.strftime("%m-%d"), (since + timedelta(days=1)).strftime("%m-%d")]
    assert charts.x_labels([date(2024, 1, 1), date(2024, 1, 31)], 30) == ["2024-01", "2024-01"]
    c = charts.build_chart("t", labels, [("Revenue", "accent", "bar", [1.0] * 30)], fmt=isk)
    # every ceil(30/6) = 5th bucket, plus the last
    assert [lbl for _x, lbl in c.x_ticks] == [labels[i] for i in (0, 5, 10, 15, 20, 25, 29)]
    # the last label wins over a neighbour it would sit on top of
    c = charts.build_chart("t", [f"{i:02d}" for i in range(37)], [("u", "accent", "bar", [1.0] * 37)], fmt=isk)
    assert [lbl for _x, lbl in c.x_ticks] == ["00", "07", "14", "21", "28", "36"]
    # edge labels are nudged inside the viewBox
    assert all(0 <= x <= c.width for x, _lbl in c.x_ticks)


# --- geometry --------------------------------------------------------------


def test_nice_ticks():
    assert charts.nice_ticks(0, 100) == [0, 20, 40, 60, 80, 100]
    assert charts.nice_ticks(-3, 5) == [-4, -2, 0, 2, 4, 6]
    assert 0 in charts.nice_ticks(-1e9, 2.5e9)
    assert charts.nice_ticks(0, 0)[0] == 0  # degenerate range still ticks


def test_build_chart_geometry_and_tooltips():
    labels = ["09-01", "09-02", "09-03", "09-04"]
    dates = [date(2026, 9, d) for d in (1, 2, 3, 4)]
    c = charts.build_chart(
        "Revenue & estimated profit",
        labels,
        [
            ("Revenue", "accent", "bar", [100.0, 200.0, 0.0, 50.0]),
            ("Est. profit", "good", "bar", [30.0, -80.0, 0.0, 10.0]),
            ("Cumulative", "good", "line", [30.0, -50.0, None, -40.0]),
        ],
        fmt=isk,
        caption="profit covers products with a cost basis only",
        x_dates=dates,
    )
    assert c.buckets == 4 and not c.empty
    assert c.width == 380 and c.height == 180
    assert c.caption.startswith("profit covers")
    assert c.legend == [("Revenue", "accent"), ("Est. profit", "good"), ("Cumulative", "good")]
    # one shared y-range including 0: ticks span both signs, 0 is a tick
    tick_values = [lbl for _y, lbl in c.y_ticks]
    assert "0" in tick_values and tick_values[0].startswith("-")
    left, top, right, bottom = c.plot
    assert top < c.zero_y < bottom
    rev, prof, cum = c.series
    assert rev.kind == "bar" and prof.kind == "bar" and cum.kind == "line"
    # grouped bars: same width, side by side in the same bucket, above 0
    assert len(rev.bars) == 4 and len(prof.bars) == 4
    assert rev.bars[0].w == prof.bars[0].w
    assert rev.bars[0].x < prof.bars[0].x < rev.bars[1].x
    assert rev.bars[0].y + rev.bars[0].h == c.zero_y
    assert rev.bars[1].h > rev.bars[0].h > rev.bars[2].h == 0
    # a negative bar hangs from the zero line in the bad tone
    neg = prof.bars[1]
    assert neg.tone == "bad" and neg.y == c.zero_y and neg.h > 0
    assert prof.bars[0].tone == "good"
    # tooltips: full ISO date, series name, fmt(v)
    assert rev.bars[1].label == "2026-09-02 · Revenue 200"
    assert neg.label == "2026-09-02 · Est. profit -80"
    # the None gap breaks the line: two M segments, three points
    assert cum.path.count("M") == 2 and cum.path.count("L") == 1
    assert len(cum.points) == 3
    assert cum.points[0].label == "2026-09-01 · Cumulative 30"
    assert cum.points[0].y < c.zero_y < cum.points[1].y
    # without x_dates the tick text is the tooltip's date
    c2 = charts.build_chart("t", labels, [("Revenue", "accent", "bar", [1.0, 2.0, 3.0, 4.0])], fmt=isk)
    assert c2.series[0].bars[0].label == "09-01 · Revenue 1"
    assert c2.series[0].bars[0].tone == "accent"


def test_build_chart_stacks_and_empty():
    labels = ["09-01", "09-02"]
    c = charts.build_chart(
        "Units sold",
        labels,
        [
            ("market", "accent", "stack", [3.0, 5.0]),
            ("contract", "dim", "stack", [2.0, 0.0]),
        ],
        fmt=lambda v: f"{v:,.0f}",
    )
    market, contract = c.series
    assert len(market.bars) == 2 and len(contract.bars) == 2
    # the second segment sits on top of the first, with the surface gap
    # trimmed from the lower segment's top edge; the stack total is exact
    b1, b2 = market.bars[0], contract.bars[0]
    assert b2.x == b1.x and b2.w == b1.w
    assert math.isclose(b2.y + b2.h, b1.y - charts.GAP, abs_tol=0.11)
    assert b2.y < b1.y and math.isclose(b1.y + b1.h, c.zero_y, abs_tol=0.11)
    # a zero segment above nothing: no gap trimmed, bar reaches the zero line
    assert math.isclose(market.bars[1].y + market.bars[1].h, c.zero_y, abs_tol=0.11)
    # the y-range covers the stack top (5 = tallest level), not just a series max
    assert [lbl for _y, lbl in c.y_ticks][-1] in ("5", "6")
    # empty only when every value is None or 0
    empty = charts.build_chart("t", labels, [("a", "accent", "bar", [0.0, None]), ("b", "dim", "line", [None, 0.0])], fmt=isk)
    assert empty.empty
    assert not charts.build_chart("t", labels, [("a", "accent", "bar", [0.0, -1.0])], fmt=isk).empty


# --- the partial -----------------------------------------------------------

TEMPLATE = '{% import "_chart.html" as ch %}{{ ch.chart(c) }}'


def render(c):
    app = template_app()
    with app.test_request_context("/ledger"):
        return render_template_string(TEMPLATE, c=c)


def test_chart_partial_tokens_only_and_empty_state():
    since, until = window(7)
    edges = charts.bucket_edges(since, until, 1)
    labels = charts.x_labels(edges, 1)
    c = charts.build_chart(
        "Revenue & estimated profit",
        labels,
        [
            ("Revenue", "accent", "bar", [1e9, 2e9, 0.0, 5e8, 3e9, 1e9, 2e9]),
            ("Est. profit", "good", "bar", [2e8, -1e8, 0.0, 1e8, 6e8, 2e8, 4e8]),
            ("Cumulative", "good", "line", [2e8, 1e8, 1e8, 2e8, 8e8, 1e9, 1.4e9]),
        ],
        fmt=isk,
        caption="profit covers products with a cost basis only",
        x_dates=edges,
    )
    html = render(c)
    assert '<figure class="chart" data-buckets="7">' in html
    assert 'viewBox="0 0 380 180"' in html
    assert 'role="img" aria-label="Revenue &amp; estimated profit"' in html
    assert "Revenue &amp; estimated profit — " in html and "cost basis only" in html
    assert html.count('<span class="swatch tone-') == 3
    svg = html[html.index("<svg") : html.index("</svg>")]
    assert "#" not in svg and "style=" not in svg
    assert svg.count("<title>") == 7 * 3
    assert svg.count("<rect ") == 14 and svg.count("<circle ") == 7 and svg.count("<path ") == 1
    assert 'class="tone-bad"' in svg and 'class="zero"' in svg
    assert svg.count('class="grid"') == len(c.y_ticks)
    assert "2026-09-02 · Est. profit -100,000,000" in svg
    # a single series gets no legend box — the title names it
    one = charts.build_chart("Cumulative estimated profit", labels, [("Cumulative", "good", "line", [1.0] * 7)], fmt=isk)
    assert 'class="legend"' not in render(one)
    # nothing to plot: the sentence, no svg
    empty = charts.build_chart("Units sold", labels, [("market", "accent", "stack", [0.0] * 7), ("contract", "dim", "stack", [None] * 7)], fmt=isk)
    html = render(empty)
    assert "No sales in this window." in html and "<svg" not in html
    assert 'data-buckets="7"' in html
