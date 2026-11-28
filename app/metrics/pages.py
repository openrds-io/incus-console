"""Graphs for the console's pages: an instance, a cluster member, a project, a list."""

from __future__ import annotations

from ..filters import filesize
from . import store
from .charts import line_chart, sparkline


def percent(value) -> str:
    return f"{value:.0f}%" if value >= 10 else f"{value:.1f}%"


def size(value) -> str:
    return filesize(value) if value else "0 B"


def rate(value) -> str:
    return f"{size(value)}/s"


def count(value) -> str:
    return f"{value:.0f}"


def cores(value) -> str:
    return f"{value:.2f} CPUs" if value < 10 else f"{value:.1f} CPUs"


def load(value) -> str:
    return f"{value:.2f}"


def _rows(data_dir: str, sid: int, read) -> list:
    conn = store.connect(data_dir)
    try:
        source = store.source_of(conn, sid)
        return read(conn, source) if source else []
    finally:
        conn.close()


def instance_charts(data_dir: str, sid: int, project: str, name: str, span: str) -> list:
    """[(title, svg)] for one instance over a range ("1h", "24h", "7d", "30d")."""
    rows = _rows(data_dir, sid, lambda c, s: store.series(c, s, project, name, span))
    if not rows:
        return []
    # The whole as it is now (a limit can change), said in the legend.
    mem_total, disk_total = rows[-1].get("mem_total"), rows[-1].get("disk_total")
    memory = [("mem", "Used (not counting cache)")]
    return [
        ("CPU", line_chart(rows, [("cpu", "CPU")], span, percent, floor=1, label="CPU")),
        ("Memory", line_chart(rows, memory, span, size, of=mem_total, label="Memory")),
        (
            "Root disk",
            line_chart(rows, [("disk", "Used")], span, size, of=disk_total, label="Root disk"),
        ),
        (
            "Disk I/O",
            line_chart(rows, [("rd", "Read"), ("wr", "Write")], span, rate, label="Disk I/O"),
        ),
        ("Network", line_chart(rows, [("rx", "In"), ("tx", "Out")], span, rate, label="Network")),
        ("Processes", line_chart(rows, [("procs", "Processes")], span, count, label="Processes")),
    ]


def member_charts(data_dir: str, sid: int, member: str, span: str) -> list:
    rows = _rows(data_dir, sid, lambda c, s: store.member_series(c, s, member, span))
    if not rows:
        return []
    cpus, mem_total = rows[-1].get("cpus"), rows[-1].get("mem_total")
    return [
        (
            "Load (1 minute)",
            line_chart(rows, [("load", "Load")], span, load, floor=1, of=cpus, label="Load"),
        ),
        (
            "Memory",
            line_chart(
                rows, [("mem", "Used (with cache)")], span, size, of=mem_total, label="Memory"
            ),
        ),
        (
            "CPU used by its instances",
            line_chart(rows, [("cores", "Instances")], span, cores, floor=1, of=cpus, label="CPU"),
        ),
        (
            "Its instances' disk I/O",
            line_chart(rows, [("rd", "Read"), ("wr", "Write")], span, rate, label="Disk I/O"),
        ),
        (
            "Its instances' network",
            line_chart(rows, [("rx", "In"), ("tx", "Out")], span, rate, label="Network"),
        ),
    ]


def project_charts(data_dir: str, sid: int, project: str, span: str) -> list:
    rows = _rows(data_dir, sid, lambda c, s: store.project_series(c, s, project, span))
    if not rows:
        return []
    return [
        ("CPU", line_chart(rows, [("cores", "All instances")], span, cores, floor=1, label="CPU")),
        ("Memory", line_chart(rows, [("mem", "All instances")], span, size, label="Memory")),
    ]


def project_top(data_dir: str, sid: int, project: str) -> list:
    """The project's instances by CPU over the last hour."""
    return _rows(data_dir, sid, lambda c, s: store.top(c, s, project))


def sparklines(data_dir: str, sid: int, project: str) -> dict:
    """{name: (cpu svg, memory svg)} over the last hour, for the instance list."""
    found = _rows(data_dir, sid, lambda c, s: store.recent(c, s, project)) or {}
    out = {}
    for name, points in found.items():
        latest = points[-1]
        cpu_title = f"CPU over the last hour, now {percent(latest.get('cpu') or 0)}"
        mem_title = f"Memory over the last hour, now {size(latest.get('mem') or 0)}"
        out[name] = (
            sparkline(points, "cpu", title=cpu_title),
            sparkline(points, "mem", top=latest.get("mem_total"), title=mem_title),
        )
    return out
