"""Metrics: reading /1.0/metrics, keeping history in metrics.db, and the graphs."""

import time

import pytest

from app.metrics import store
from app.metrics.collect import Collector, instance_totals, values
from app.metrics.prom import parse

from .test_app import text

CONFIG = {
    "METRICS_INTERVAL": 15,
    "METRICS_RAW_HOURS": 24,
    "METRICS_5M_DAYS": 7,
    "METRICS_1H_DAYS": 30,
}


def test_parse_reads_samples_and_skips_the_rest():
    found = list(
        parse(
            "# HELP x Some help\n"
            "# TYPE x counter\n"
            'incus_procs_total{name="c1",project="default"} 12\n'
            'odd{name="a \\"quoted\\" one"} 1.5 1700000000\n'
            "no_labels 3\n"
            "broken line here\n"
        )
    )
    assert found == [
        ("incus_procs_total", {"name": "c1", "project": "default"}, 12.0),
        ("odd", {"name": 'a "quoted" one'}, 1.5),
        ("no_labels", {}, 3.0),
    ]


def test_rates_and_levels(fake):
    fake.add_instance("c1")
    first = instance_totals(fake._metrics())[("default", "c1")]
    second = instance_totals(fake._metrics())[("default", "c1")]
    row = values(second, first, 15)
    assert row["cpu"] == pytest.approx(10.0)  # 3 s busy per 15 s, over 2 CPUs
    assert row["rx"] == pytest.approx(40.0)  # eth0 only, not lo
    assert row["wr"] == pytest.approx(200.0)
    assert row["mem"] == 256 * 1024**2 and row["mem_total"] == 1024**3
    assert row["disk"] == 2 * 1024**3 and row["procs"] == 12
    assert "cpu" not in values(first, None, 0)  # one sample is no rate yet
    restarted = values(first, second, 15)
    assert restarted.get("cpu") is None and restarted["rx"] is None


# Half past a minute, two minutes ago: the second and third rounds fall in
# different minutes, which the list's small graphs need for two points.
START = int(time.time()) // 60 * 60 - 120 + 30


@pytest.fixture
def collected(app, sid, fake):
    """Three rounds 15 s apart, a minute ago, with c1 running and c2 stopped."""
    fake.add_instance("c1")
    fake.add_instance("c2", status="Stopped")
    with app.app_context():
        conn = store.connect(app.config["DATA_DIR"])
        collector = Collector(conn, CONFIG)
        for moment in (START, START + 15, START + 30):
            collector.clock = lambda moment=moment: float(moment)
            collector.cycle()
        yield conn


def test_a_round_keeps_one_row_per_running_instance(collected, sid):
    source = store.source_of(collected, sid)
    assert source
    rows = collected.execute("SELECT name, ts, cpu, rx FROM samples_raw ORDER BY ts").fetchall()
    assert [(r["name"], r["ts"]) for r in rows] == [("c1", START + i) for i in (0, 15, 30)]
    assert rows[0]["cpu"] is None and rows[1]["cpu"] == pytest.approx(10.0)


def test_two_registrations_of_one_server_are_sampled_once(app, browser, sid, fake, socket_path):
    browser.post("/servers", {"kind": "unix", "name": "Again", "address": socket_path})
    fake.add_instance("c1")
    with app.app_context():
        conn = store.connect(app.config["DATA_DIR"])
        Collector(conn, CONFIG).cycle()
        assert fake.metric_calls == 1
        sources = {r["source"] for r in conn.execute("SELECT source FROM sources")}
        assert len(sources) == 1


def test_a_cluster_is_sampled_member_by_member(app, sid, fake):
    fake.add_instance("a")
    fake.add_instance("b")
    fake.make_cluster("node1", "node2")
    fake.instances["b"]["location"] = "node2"
    with app.app_context():
        conn = store.connect(app.config["DATA_DIR"])
        Collector(conn, CONFIG).cycle()
        names = [r["name"] for r in conn.execute("SELECT name FROM samples_raw ORDER BY name")]
    assert names == ["a", "b"] and fake.metric_calls == 2


def test_rolling_up_and_thinning(tmp_path):
    conn = store.connect(str(tmp_path))
    now = 10_000_000 - (10_000_000 % 3600) + 600
    for i, cpu in enumerate([10, 30, 20, 90]):
        store.add(conn, "s", "default", "c1", now - 300 + i * 60, {"cpu": cpu, "procs": 5})
    store.add(conn, "s", "default", "c1", now - 40 * 86400, {"cpu": 1})  # far too old
    conn.commit()
    store.maintain(conn, CONFIG, now=now)
    (five,) = conn.execute("SELECT * FROM samples_5m").fetchall()
    assert five["cpu"] == pytest.approx(37.5) and five["cpu_max"] == 90
    (hour,) = conn.execute("SELECT * FROM samples_1h").fetchall()
    assert hour["cpu_max"] == 90
    assert conn.execute("SELECT COUNT(*) FROM samples_raw WHERE cpu = 1").fetchone()[0] == 0
    rows = store.series(conn, "s", "default", "c1", "24h", now=now)
    assert rows and rows[-1]["cpu_max"] == 90


def test_the_instance_page_draws_the_history(collected, browser, sid):
    page = text(browser.get(f"/servers/{sid}/instances/c1"))
    assert page.count('<svg class="chart"') == 6
    for title in ("CPU", "Memory", "Root disk", "Disk I/O", "Network", "Processes"):
        assert f"<figcaption>{title}</figcaption>" in page
    assert "Now:" in page and "40 B/s" in page
    assert "range=24h" in page and "<strong>1h</strong>" in page


def test_no_history_yet(browser, sid, fake):
    fake.add_instance("c9")
    page = text(browser.get(f"/servers/{sid}/instances/c9?range=bogus"))
    assert "No history yet" in page and "<strong>1h</strong>" in page


def test_an_older_metrics_db_gets_the_new_columns(tmp_path):
    import sqlite3

    old = sqlite3.connect(tmp_path / "metrics.db")
    old.execute(
        "CREATE TABLE samples_raw (source TEXT NOT NULL, project TEXT NOT NULL, "
        "name TEXT NOT NULL, ts INTEGER NOT NULL, cpu REAL, "
        "PRIMARY KEY (source, project, name, ts)) WITHOUT ROWID"
    )
    old.close()
    conn = store.connect(str(tmp_path))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(samples_raw)")}
    assert {"cores", "mem", "procs"} <= columns


def test_the_list_shows_the_last_hour(collected, browser, sid):
    page = text(browser.get(f"/servers/{sid}/instances"))
    assert page.count('<svg class="sparkline"') == 2  # c1's CPU and memory
    assert "CPU over the last hour, now 10%" in page


def test_the_project_page_adds_up_its_instances(collected, browser, sid):
    page = text(browser.get(f"/servers/{sid}/projects/default"))
    assert "<figcaption>CPU</figcaption>" in page and "<figcaption>Memory</figcaption>" in page
    assert "Busiest in the last hour" in page and "0.20 CPUs" in page


def test_a_cluster_member_page_has_its_history(app, browser, sid, fake):
    fake.add_instance("a")
    fake.make_cluster("node1", "node2")
    with app.app_context():
        conn = store.connect(app.config["DATA_DIR"])
        collector = Collector(conn, CONFIG)
        for moment in (START, START + 15, START + 30):
            collector.clock = lambda moment=moment: float(moment)
            collector.cycle()
        query = "SELECT * FROM member_raw WHERE member = 'node1' ORDER BY ts DESC"
        row = conn.execute(query).fetchone()
    # The fake's counters move on with every call, and each round asks two members.
    assert row["instances"] == 1 and row["cores"] == pytest.approx(0.4)
    assert row["load"] == 0.5 and row["cpus"] == 4 and row["mem"] == 2 * 1024**3
    page = text(browser.get(f"/servers/{sid}/cluster/members/node1"))
    for title in ("Load (1 minute)", "Memory", "CPU used by its instances"):
        assert f"<figcaption>{title}</figcaption>" in page
    assert "</span> of 4.00" in page  # load, of 4 CPUs


def test_a_member_without_metrics_still_has_its_load(app, sid, fake, caplog):
    fake.make_cluster("node1", "node2")
    fake.pools.clear()  # Incus then refuses /1.0/metrics altogether
    with app.app_context():
        conn = store.connect(app.config["DATA_DIR"])
        collector = Collector(conn, CONFIG)
        for moment in (START, START + 15):
            collector.clock = lambda moment=moment: float(moment)
            collector.cycle()
        rows = conn.execute("SELECT member, load, instances FROM member_raw").fetchall()
    assert sorted((r["member"], r["load"], r["instances"]) for r in rows) == [
        ("node1", 0.5, 0),
        ("node1", 0.5, 0),
        ("node2", 0.5, 0),
        ("node2", 0.5, 0),
    ]
    warnings = [r.message for r in caplog.records if "No metrics from" in r.message]
    assert warnings == [
        "No metrics from node1: Failed loading storage pools: Storage pool(s) not found",
        "No metrics from node2: Failed loading storage pools: Storage pool(s) not found",
    ]  # said once each, not every round


def test_a_member_s_cpu_count_comes_from_its_resources(app, sid, fake):
    fake.make_cluster("node1", "node2")
    fake.cpus_in_state = False
    with app.app_context():
        conn = store.connect(app.config["DATA_DIR"])
        Collector(conn, CONFIG).cycle()
        cpus = {r["member"]: r["cpus"] for r in conn.execute("SELECT * FROM member_raw")}
    assert cpus == {"node1": 4, "node2": 4}
