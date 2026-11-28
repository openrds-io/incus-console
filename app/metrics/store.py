"""metrics.db: one row per instance (and per cluster member) per moment, at three resolutions.

samples_raw / member_raw  every sample (METRICS_INTERVAL), for METRICS_RAW_HOURS
samples_5m  / member_5m   5-minute averages and peaks, for METRICS_5M_DAYS
samples_1h  / member_1h   1-hour averages and peaks, for METRICS_1H_DAYS

A row is keyed by its source (the certificate fingerprint of the server or
cluster it came from, so two registrations of one cluster share history), the
instance's project and name or the member's name, and time (Unix seconds, the
start of its period).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# An instance: levels, and rates per second. cores is CPU seconds per second, so
# it adds up across instances; cpu is the same as a share of the instance's CPUs.
FIELDS = (
    "cpu", "cores", "mem", "mem_total", "disk", "disk_total", "rd", "wr", "rx", "tx", "procs",
)  # fmt: skip
PEAKS = ("cpu", "cores", "mem", "rd", "wr", "rx", "tx")
# A cluster member: its own load and memory, and what its instances use together.
MEMBER_FIELDS = (
    "load", "cpus", "mem", "mem_total", "cores", "rd", "wr", "rx", "tx", "instances",
)  # fmt: skip
MEMBER_PEAKS = ("load", "mem", "cores", "rd", "wr", "rx", "tx")

FAMILIES = {
    "instance": {
        "keys": ("project", "name"),
        "fields": FIELDS,
        "peaks": PEAKS,
        "tables": {"raw": "samples_raw", "5m": "samples_5m", "1h": "samples_1h"},
    },
    "member": {
        "keys": ("member",),
        "fields": MEMBER_FIELDS,
        "peaks": MEMBER_PEAKS,
        "tables": {"raw": "member_raw", "5m": "member_5m", "1h": "member_1h"},
    },
}
TABLES = FAMILIES["instance"]["tables"]
WIDTHS = {"5m": 300, "1h": 3600}

# A graph's range: which table it reads, how long it spans, and how wide each point is.
RANGES = {
    "1h": ("raw", 3600, 15),
    "24h": ("5m", 86400, 300),
    "7d": ("5m", 7 * 86400, 1800),
    "30d": ("1h", 30 * 86400, 3600),
}


def _columns(family: dict, peaked: bool) -> list[str]:
    columns = list(family["fields"])
    if peaked:
        columns += [f"{f}_max" for f in family["peaks"]]
    return columns


def _create(conn, family: dict) -> None:
    keys = ", ".join(f"{k} TEXT NOT NULL" for k in family["keys"])
    for tier, table in family["tables"].items():
        columns = _columns(family, tier != "raw")
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} (source TEXT NOT NULL, {keys}, "
            f"ts INTEGER NOT NULL, {', '.join(c + ' REAL' for c in columns)}, "
            f"PRIMARY KEY (source, {', '.join(family['keys'])}, ts)) WITHOUT ROWID"
        )
        # A table from an older console lacks the columns added since.
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} REAL")


def connect(data_dir: str) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(data_dir) / "metrics.db", timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for family in FAMILIES.values():
        _create(conn, family)
    # Which source each registered server's history is under.
    conn.execute("CREATE TABLE IF NOT EXISTS sources (sid INTEGER PRIMARY KEY, source TEXT)")
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


def add_row(conn, kind: str, source: str, keys: tuple, ts: int, values: dict) -> None:
    family = FAMILIES[kind]
    names = ", ".join((*family["keys"], "ts", *family["fields"]))
    marks = ", ".join("?" for _ in range(1 + len(family["keys"]) + 1 + len(family["fields"])))
    conn.execute(
        f"INSERT OR REPLACE INTO {family['tables']['raw']} (source, {names}) VALUES ({marks})",
        (source, *keys, ts, *(values.get(f) for f in family["fields"])),
    )


def add(conn, source: str, project: str, name: str, ts: int, values: dict) -> None:
    add_row(conn, "instance", source, (project, name), ts, values)


def _roll(conn, family: dict, src: str, dst: str, width: int, since: int, peaked: bool) -> None:
    """Average (and peak) the rows of src into dst periods of width seconds."""
    keys = ", ".join(family["keys"])
    averages = ", ".join(f"AVG({f})" for f in family["fields"])
    peaks = ", ".join(f"MAX({f}_max)" if peaked else f"MAX({f})" for f in family["peaks"])
    columns = ", ".join(_columns(family, True))
    conn.execute(
        f"INSERT OR REPLACE INTO {dst} (source, {keys}, ts, {columns}) "
        f"SELECT source, {keys}, (ts / {width}) * {width} AS period, {averages}, {peaks} "
        f"FROM {src} WHERE ts >= ? GROUP BY source, {keys}, period",
        ((since // width) * width,),
    )


def maintain(conn, config: dict, now: int | None = None) -> None:
    """Roll recent rows up, and drop what is older than it is kept for."""
    now = int(now or time.time())
    keep = {
        "raw": config["METRICS_RAW_HOURS"] * 3600,
        "5m": config["METRICS_5M_DAYS"] * 86400,
        "1h": config["METRICS_1H_DAYS"] * 86400,
    }
    five, hour = WIDTHS["5m"], WIDTHS["1h"]
    for family in FAMILIES.values():
        tables = family["tables"]
        # The last two periods of each: the current one is still filling.
        _roll(conn, family, tables["raw"], tables["5m"], five, now - 2 * five, False)
        _roll(conn, family, tables["5m"], tables["1h"], hour, now - 2 * hour, True)
        for tier, seconds in keep.items():
            conn.execute(f"DELETE FROM {tables[tier]} WHERE ts < ?", (now - seconds,))
    conn.commit()


def _series(conn, kind: str, source: str, keys: tuple, span: str, now: int | None) -> list:
    family = FAMILIES[kind]
    tier, length, step = RANGES[span]
    now = int(now or time.time())
    averages = ", ".join(f"AVG({f}) AS {f}" for f in family["fields"])
    suffix = "" if tier == "raw" else "_max"
    peaks = ", ".join(f"MAX({f}{suffix}) AS {f}_max" for f in family["peaks"])
    where = " AND ".join(f"{k} = ?" for k in family["keys"])
    rows = conn.execute(
        f"SELECT (ts / {step}) * {step} AS t, {averages}, {peaks} "
        f"FROM {family['tables'][tier]} WHERE source = ? AND {where} AND ts >= ? "
        "GROUP BY t ORDER BY t",
        (source, *keys, now - length),
    ).fetchall()
    return [dict(row) for row in rows]


def series(conn, source: str, project: str, name: str, span: str, now: int | None = None):
    """An instance's rows of {t, field: average, field_max: peak}, oldest first."""
    return _series(conn, "instance", source, (project, name), span, now)


def member_series(conn, source: str, member: str, span: str, now: int | None = None):
    return _series(conn, "member", source, (member,), span, now)


def project_series(conn, source: str, project: str, span: str, now: int | None = None):
    """A project's totals: CPU cores and memory of its instances added up, per point."""
    tier, length, step = RANGES[span]
    now = int(now or time.time())
    rows = conn.execute(
        "SELECT t, SUM(cores) AS cores, SUM(mem) AS mem, COUNT(*) AS instances FROM ("
        f"SELECT name, (ts / {step}) * {step} AS t, AVG(cores) AS cores, AVG(mem) AS mem "
        f"FROM {TABLES[tier]} WHERE source = ? AND project = ? AND ts >= ? GROUP BY name, t"
        ") GROUP BY t ORDER BY t",
        (source, project, now - length),
    ).fetchall()
    return [dict(row) for row in rows]


def recent(conn, source: str, project: str, seconds: int = 3600, now: int | None = None):
    """{name: [{t, cpu, mem, mem_total}]} per minute, for the small graphs in lists."""
    now = int(now or time.time())
    found: dict[str, list] = {}
    for row in conn.execute(
        "SELECT name, (ts / 60) * 60 AS t, AVG(cpu) AS cpu, AVG(mem) AS mem, "
        f"MAX(mem_total) AS mem_total FROM {TABLES['raw']} "
        "WHERE source = ? AND project = ? AND ts >= ? GROUP BY name, t ORDER BY name, t",
        (source, project, now - seconds),
    ):
        found.setdefault(row["name"], []).append(dict(row))
    return found


def top(conn, source: str, project: str, seconds: int = 3600, now: int | None = None):
    """The project's instances by CPU over the last hour: name, cores, cpu, mem, mem_total."""
    now = int(now or time.time())
    rows = conn.execute(
        "SELECT name, AVG(cores) AS cores, AVG(cpu) AS cpu, AVG(mem) AS mem, "
        f"MAX(mem_total) AS mem_total FROM {TABLES['raw']} "
        "WHERE source = ? AND project = ? AND ts >= ? GROUP BY name "
        "ORDER BY AVG(cores) DESC, AVG(mem) DESC",
        (source, project, now - seconds),
    ).fetchall()
    return [dict(row) for row in rows]
