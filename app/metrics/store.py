"""metrics.db: one row per instance per moment, at three resolutions.

samples_raw  every sample (METRICS_INTERVAL), for METRICS_RAW_HOURS
samples_5m   5-minute averages and peaks, for METRICS_5M_DAYS
samples_1h   1-hour averages and peaks, for METRICS_1H_DAYS

A row is keyed by its source (the certificate fingerprint of the server or
cluster it came from, so two registrations of one cluster share history),
project, instance name and time (Unix seconds, the start of its period).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# Levels and rates per row; the rates are per second.
FIELDS = ("cpu", "mem", "mem_total", "disk", "disk_total", "rd", "wr", "rx", "tx", "procs")
# Rolled-up rows also keep the highest value of these within their period.
PEAKS = ("cpu", "mem", "rd", "wr", "rx", "tx")
TABLES = {"raw": "samples_raw", "5m": "samples_5m", "1h": "samples_1h"}
WIDTHS = {"5m": 300, "1h": 3600}


def _table(name: str, peaks: bool) -> str:
    columns = ", ".join(f"{f} REAL" for f in FIELDS)
    if peaks:
        columns += ", " + ", ".join(f"{f}_max REAL" for f in PEAKS)
    return (
        f"CREATE TABLE IF NOT EXISTS {name} (source TEXT NOT NULL, project TEXT NOT NULL, "
        f"name TEXT NOT NULL, ts INTEGER NOT NULL, {columns}, "
        "PRIMARY KEY (source, project, name, ts)) WITHOUT ROWID"
    )


SCHEMA = [
    _table(TABLES["raw"], False),
    _table(TABLES["5m"], True),
    _table(TABLES["1h"], True),
    # Which source each registered server's history is under.
    "CREATE TABLE IF NOT EXISTS sources (sid INTEGER PRIMARY KEY, source TEXT NOT NULL)",
]


def connect(data_dir: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(data_dir) / "metrics.db", timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for statement in SCHEMA:
        conn.execute(statement)
    conn.commit()
    return conn


def set_source(conn, sid: int, source: str) -> None:
    conn.execute(
        "INSERT INTO sources (sid, source) VALUES (?, ?) "
        "ON CONFLICT (sid) DO UPDATE SET source = excluded.source",
        (sid, source),
    )


def source_of(conn, sid: int) -> str | None:
    row = conn.execute("SELECT source FROM sources WHERE sid = ?", (sid,)).fetchone()
    return row["source"] if row else None


def add(conn, source: str, project: str, name: str, ts: int, values: dict) -> None:
    columns = ", ".join(FIELDS)
    marks = ", ".join("?" for _ in FIELDS)
    conn.execute(
        f"INSERT OR REPLACE INTO {TABLES['raw']} (source, project, name, ts, {columns}) "
        f"VALUES (?, ?, ?, ?, {marks})",
        (source, project, name, ts, *(values.get(f) for f in FIELDS)),
    )


def _roll(conn, src: str, dst: str, width: int, since: int, peaked: bool) -> None:
    """Average (and peak) the rows of src into dst periods of width seconds."""
    averages = ", ".join(f"AVG({f})" for f in FIELDS)
    peaks = ", ".join(f"MAX({f}_max)" if peaked else f"MAX({f})" for f in PEAKS)
    columns = ", ".join(FIELDS) + ", " + ", ".join(f"{f}_max" for f in PEAKS)
    conn.execute(
        f"INSERT OR REPLACE INTO {dst} (source, project, name, ts, {columns}) "
        f"SELECT source, project, name, (ts / {width}) * {width} AS period, {averages}, {peaks} "
        f"FROM {src} WHERE ts >= ? GROUP BY source, project, name, period",
        ((since // width) * width,),
    )


def maintain(conn, config: dict, now: int | None = None) -> None:
    """Roll recent rows up, and drop what is older than it is kept for."""
    now = int(now or time.time())
    # The last two periods of each: the current one is still filling.
    _roll(conn, TABLES["raw"], TABLES["5m"], WIDTHS["5m"], now - 2 * WIDTHS["5m"], False)
    _roll(conn, TABLES["5m"], TABLES["1h"], WIDTHS["1h"], now - 2 * WIDTHS["1h"], True)
    keep = {
        "raw": config["METRICS_RAW_HOURS"] * 3600,
        "5m": config["METRICS_5M_DAYS"] * 86400,
        "1h": config["METRICS_1H_DAYS"] * 86400,
    }
    for tier, seconds in keep.items():
        conn.execute(f"DELETE FROM {TABLES[tier]} WHERE ts < ?", (now - seconds,))
    conn.commit()


# A graph's range: which table it reads, how long it spans, and how wide each point is.
RANGES = {
    "1h": ("raw", 3600, 15),
    "24h": ("5m", 86400, 300),
    "7d": ("5m", 7 * 86400, 1800),
    "30d": ("1h", 30 * 86400, 3600),
}


def series(conn, source: str, project: str, name: str, span: str, now: int | None = None):
    """Rows of {ts, field: average, field_max: peak} over the range, oldest first."""
    tier, length, step = RANGES[span]
    now = int(now or time.time())
    table = TABLES[tier]
    averages = ", ".join(f"AVG({f}) AS {f}" for f in FIELDS)
    if tier == "raw":
        peaks = ", ".join(f"MAX({f}) AS {f}_max" for f in PEAKS)
    else:
        peaks = ", ".join(f"MAX({f}_max) AS {f}_max" for f in PEAKS)
    rows = conn.execute(
        f"SELECT (ts / {step}) * {step} AS t, {averages}, {peaks} FROM {table} "
        "WHERE source = ? AND project = ? AND name = ? AND ts >= ? GROUP BY t ORDER BY t",
        (source, project, name, now - length),
    ).fetchall()
    return [dict(row) for row in rows]
