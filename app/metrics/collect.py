"""One round of sampling: every registered server, and every member of each cluster."""

from __future__ import annotations

import time

from .. import servers
from ..incus import IncusError
from . import prom, store

# Counters, whose change per second is what is kept.
COUNTERS = ("busy", "rd", "wr", "rx", "tx")


def instance_totals(text: str) -> dict[tuple[str, str], dict]:
    """(project, name): this moment's counters and levels, from /1.0/metrics."""
    found: dict[tuple[str, str], dict] = {}
    for metric, labels, value in prom.parse(text):
        if labels.get("type") == "virtual-machine" or "name" not in labels:
            continue
        row = found.setdefault(
            (labels.get("project", "default"), labels["name"]),
            {"busy": 0.0, "rd": 0.0, "wr": 0.0, "rx": 0.0, "tx": 0.0},
        )
        if metric == "incus_cpu_seconds_total":
            if labels.get("mode") != "idle":
                row["busy"] += value
        elif metric == "incus_cpu_effective_total":
            row["cpus"] = value
        elif metric == "incus_memory_MemTotal_bytes":
            row["mem_total"] = value
        elif metric == "incus_memory_MemAvailable_bytes":
            row["mem_avail"] = value
        elif metric in ("incus_filesystem_size_bytes", "incus_filesystem_avail_bytes"):
            if labels.get("mountpoint") == "/" or labels.get("device") == "root":
                row["disk_size" if "size" in metric else "disk_avail"] = value
        elif metric == "incus_disk_read_bytes_total":
            row["rd"] += value
        elif metric == "incus_disk_written_bytes_total":
            row["wr"] += value
        elif metric in ("incus_network_receive_bytes_total", "incus_network_transmit_bytes_total"):
            if labels.get("device") != "lo":
                row["rx" if "receive" in metric else "tx"] += value
        elif metric == "incus_procs_total":
            row["procs"] = value
    return found


def values(now: dict, before: dict | None, seconds: float) -> dict:
    """The row to keep: levels as they are, counters as a rate since the last sample."""
    out: dict = {"procs": now.get("procs")}
    if "mem_total" in now and "mem_avail" in now:
        out["mem_total"] = now["mem_total"]
        out["mem"] = max(0.0, now["mem_total"] - now["mem_avail"])
    if "disk_size" in now and "disk_avail" in now:
        out["disk_total"] = now["disk_size"]
        out["disk"] = max(0.0, now["disk_size"] - now["disk_avail"])
    if before is None or seconds <= 0:
        return out
    rates = {}
    for key in COUNTERS:
        change = now[key] - before[key]
        rates[key] = change / seconds if change >= 0 else None  # restarted: no rate
    if rates["busy"] is not None:
        cpus = now.get("cpus") or 1
        out["cpu"] = min(100.0, max(0.0, 100 * rates["busy"] / cpus))
    for key in ("rd", "wr", "rx", "tx"):
        out[key] = rates[key]
    return out


class Collector:
    """Keeps the last counters of each instance between rounds, to make rates."""

    def __init__(self, conn, config):
        self.conn = conn
        self.config = config
        self.last: dict[tuple, tuple[float, dict]] = {}
        self.rounds = 0
        self.clock = time.time

    def _texts(self, client, info: dict) -> list[str]:
        """/1.0/metrics of the server, or of each online member of its cluster."""
        if not (info.get("environment") or {}).get("server_clustered"):
            return [client.metrics_text()]
        texts = []
        for member in client.cluster_members():
            if member.get("status") == "Online":
                try:
                    texts.append(client.metrics_text(target=member["server_name"]))
                except IncusError:
                    continue
        return texts

    def cycle(self) -> None:
        done: set[str] = set()
        for row in servers.all_servers():
            client = servers.client_for(row)
            client.timeout = min(client.timeout, self.config["METRICS_INTERVAL"])
            try:
                info = client.server_info()
                env = info.get("environment") or {}
                # Two registrations of one cluster are sampled once, kept once.
                source = env.get("certificate_fingerprint") or f"server-{row['id']}"
                store.set_source(self.conn, row["id"], source)
                if source in done:
                    continue
                done.add(source)
                texts = self._texts(client, info)
            except IncusError:
                continue
            now = self.clock()
            ts = int(now)
            for text in texts:
                for (project, name), totals in instance_totals(text).items():
                    key = (source, project, name)
                    before = self.last.get(key)
                    seconds = now - before[0] if before else 0
                    row = values(totals, before[1] if before else None, seconds)
                    store.add(self.conn, source, project, name, ts, row)
                    self.last[key] = (now, totals)
        self.conn.commit()
        self.rounds += 1
        # Rolling up and pruning once a minute is plenty.
        if self.rounds % max(1, 60 // self.config["METRICS_INTERVAL"]) == 0:
            store.maintain(self.conn, self.config)
