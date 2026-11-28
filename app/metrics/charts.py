"""Small line graphs as inline SVG, drawn on the server: no charting library."""

from __future__ import annotations

import datetime
import math

from markupsafe import Markup, escape

# Close to the size a graph is shown at, so its labels stay readable.
WIDTH, HEIGHT = 360, 110
PAD_LEFT, PAD_BOTTOM, PAD_TOP = 2, 16, 14
COLOURS = ("var(--chart-1, #0e8420)", "var(--chart-2, #2c6ecb)")


def _when(ts: int, span: str) -> str:
    moment = datetime.datetime.fromtimestamp(ts)
    return moment.strftime("%H:%M" if span in ("1h", "24h") else "%b %d")


def nice_top(value: float) -> float:
    """A round number at or above value, for the top of the scale: 1, 2, 2.5, 5 x 10^n."""
    if value <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if step * power >= value:
            return step * power
    return 10 * power


def line_chart(
    rows, lines, span: str, fmt, floor: float = 0, of: float | None = None, label: str = ""
) -> Markup:
    """lines: [(field, legend)]. The scale fits the data (at least floor); of, when
    given, is the whole the values are part of, said in the legend. A faint line
    shows each period's peak where the table keeps one."""
    points = [r for r in rows if any(r.get(f) is not None for f, _ in lines)]
    if len(points) < 2:
        return Markup('<p class="u-text--muted chart-empty">Not enough history yet.</p>')
    first, last = points[0]["t"], points[-1]["t"]
    highest = max(
        (r.get(f"{f}_max") if r.get(f"{f}_max") is not None else r.get(f)) or 0
        for r in points
        for f, _ in lines
    )
    top = nice_top(max(floor, highest * 1.1))
    plot_w, plot_h = WIDTH - PAD_LEFT, HEIGHT - PAD_BOTTOM - PAD_TOP

    def x(ts):
        return PAD_LEFT + (ts - first) / max(1, last - first) * plot_w

    def y(value):
        return PAD_TOP + plot_h - (value / top) * plot_h

    parts = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
        f'aria-label="{escape(label)}">',
        f'<line class="chart-axis" x1="{PAD_LEFT}" y1="{PAD_TOP + plot_h}" '
        f'x2="{WIDTH}" y2="{PAD_TOP + plot_h}"/>',
    ]
    for index, (field, _legend) in enumerate(lines):
        colour = COLOURS[index % len(COLOURS)]
        peak = [r for r in points if r.get(f"{field}_max") is not None]
        if peak and any(r[f"{field}_max"] != r.get(field) for r in peak):
            coords = " ".join(f"{x(r['t']):.1f},{y(r[f'{field}_max']):.1f}" for r in peak)
            parts.append(f'<polyline class="chart-peak" points="{coords}" stroke="{colour}"/>')
        values = [r for r in points if r.get(field) is not None]
        coords = " ".join(f"{x(r['t']):.1f},{y(r[field]):.1f}" for r in values)
        parts.append(f'<polyline class="chart-line" points="{coords}" stroke="{colour}"/>')
    parts += [
        f'<text class="chart-label" x="{PAD_LEFT + 2}" y="10">{escape(fmt(top))}</text>',
        f'<text class="chart-label" x="{PAD_LEFT + 2}" y="{HEIGHT - 4}">'
        f"{escape(_when(first, span))}</text>",
        f'<text class="chart-label" x="{WIDTH - 2}" y="{HEIGHT - 4}" text-anchor="end">'
        f"{escape(_when(last, span))}</text>",
        "</svg>",
    ]
    latest = points[-1]
    legend = " · ".join(
        f'<span class="chart-key" style="--key:{COLOURS[i % len(COLOURS)]}">'
        f"{escape(name)} {escape(fmt(latest.get(f) or 0))}</span>"
        for i, (f, name) in enumerate(lines)
    )
    # Every value that is not a number made here went through escape() above.
    if of:
        legend += f" of {escape(fmt(of))}"
    return Markup("".join(parts) + f'<p class="chart-legend">Now: {legend}</p>')  # noqa: S704
