"""The graphs on an instance's page."""

from __future__ import annotations

from ..filters import filesize
from . import store
from .charts import line_chart


def percent(value) -> str:
    return f"{value:.0f}%" if value >= 10 else f"{value:.1f}%"


def size(value) -> str:
    return filesize(value) if value else "0 B"


def rate(value) -> str:
    return f"{size(value)}/s"


def count(value) -> str:
    return f"{value:.0f}"


def instance_charts(data_dir: str, sid: int, project: str, name: str, span: str) -> list:
    """[(title, svg)] for one instance over a range ("1h", "24h", "7d", "30d")."""
    conn = store.connect(data_dir)
    try:
        source = store.source_of(conn, sid)
        rows = store.series(conn, source, project, name, span) if source else []
    finally:
        conn.close()
    if not rows:
        return []
    # The whole as it is now (a limit can change), said in the legend.
    mem_total = rows[-1].get("mem_total")
    disk_total = rows[-1].get("disk_total")
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
