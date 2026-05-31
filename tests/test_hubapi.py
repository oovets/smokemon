"""S2 Prometheus exposition + S3 JSON API (latest / fleet / heatmap). Seeds a small
two-node DB and checks the read-only query layer behind the hub's GET endpoints."""

from smokemon import core, hubapi, schema


def _ping_rows(ts0, target, losses, med):
    rows = []
    for i, loss in enumerate(losses):
        m = None if loss >= 100 else med
        rows.append({"ts": ts0 + i * 10, "target": target, "sent": 20,
                     "recv": 0 if loss >= 100 else 20, "loss_pct": loss,
                     "rtt_min": m, "rtt_p25": m, "rtt_median": m, "rtt_p75": m,
                     "rtt_avg": m, "rtt_max": m, "rtt_stddev": 0.1})
    return rows


def _seed(conn, ts0):
    schema.init_node(conn)
    # app01: clean internet + gateway.
    schema.insert(conn, "ping_runs", _ping_rows(ts0, "1.1.1.1", [0.0] * 6, 8.0), node="app01")
    schema.insert(conn, "ping_runs", _ping_rows(ts0, "192.168.0.1", [0.0] * 6, 1.0), node="app01")
    schema.insert(conn, "host_samples", [{
        "ts": ts0 + 50, "cpu_pct": 22.0, "load1": 0.5, "load5": 0.5, "load15": 0.5,
        "mem_used_pct": 41.0, "mem_total_mb": 4000.0, "temp_c": 46.0}], node="app01")
    # pi01: internet fully down for 4 cycles, gateway clean -> isp-outage.
    schema.insert(conn, "ping_runs",
                  _ping_rows(ts0, "1.1.1.1", [0.0, 100.0, 100.0, 100.0, 100.0, 0.0], 9.0), node="pi01")
    schema.insert(conn, "ping_runs", _ping_rows(ts0, "192.168.0.1", [0.0] * 6, 2.0), node="pi01")
    schema.insert(conn, "host_samples", [{
        "ts": ts0 + 50, "cpu_pct": 80.0, "load1": 2.0, "load5": 2.0, "load15": 2.0,
        "mem_used_pct": 70.0, "mem_total_mb": 2000.0, "temp_c": 70.0}], node="pi01")
    conn.commit()


def test_nodes_and_latest(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    assert hubapi.nodes(conn) == ["app01", "pi01"]
    latest = hubapi.latest_metrics(conn)
    assert latest["app01"]["cpu"] == 22.0
    assert latest["app01"]["targets"]["1.1.1.1"]["rtt_ms"] == 8.0
    assert latest["pi01"]["temp"] == 70.0
    conn.close()


def test_prometheus_exposition(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    text = hubapi.prometheus(conn)
    assert "# TYPE smokemon_ping_rtt_ms gauge" in text
    assert 'smokemon_cpu_pct{node="app01"} 22.0' in text
    assert 'smokemon_ping_loss_pct{node="pi01",target="1.1.1.1"}' in text
    # every metric line is well-formed name{labels} value
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert "{" in line and "}" in line and " " in line.rsplit("}", 1)[1]
    conn.close()


def test_fleet_ranks_worst_first(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    fleet = hubapi.fleet(conn, hours=24, until=ts0 + 100)
    assert [r["node"] for r in fleet] == ["pi01", "app01"]   # pi01 has the outage
    pi = next(r for r in fleet if r["node"] == "pi01")
    assert pi["uptime_pct"] is not None and pi["uptime_pct"] < 100.0
    assert pi["incidents"] >= 1 and pi["downtime_s"] > 0
    app = next(r for r in fleet if r["node"] == "app01")
    assert app["uptime_pct"] == 100.0 and app["downtime_s"] == 0
    conn.close()


def test_fleet_status_states(tmp_db, ts0):
    """fleet_status derives state from the *latest* sample (no incident detection) and
    sorts worst-first: down → stale → warn → healthy."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    base = ts0
    schema.insert(conn, "ping_runs", _ping_rows(base, "1.1.1.1", [0.0, 0.0, 0.0], 8.0), node="ok01")
    schema.insert(conn, "ping_runs", _ping_rows(base, "1.1.1.1", [0.0, 0.0, 100.0], 9.0), node="down01")
    schema.insert(conn, "ping_runs", _ping_rows(base, "1.1.1.1", [0.0, 0.0, 0.0], 300.0), node="warn01")
    schema.insert(conn, "ping_runs", _ping_rows(base - 1000, "1.1.1.1", [0.0, 0.0, 0.0], 7.0), node="stale01")
    conn.commit()
    fs = hubapi.fleet_status(conn, stale_after_s=300.0, now=base + 10)
    state = {n["node"]: n["state"] for n in fs["nodes"]}
    assert state == {"ok01": "healthy", "down01": "down", "warn01": "warn", "stale01": "stale"}
    assert fs["counts"] == {"healthy": 1, "warn": 1, "down": 1, "stale": 1}
    assert [n["node"] for n in fs["nodes"]] == ["down01", "stale01", "warn01", "ok01"]
    conn.close()


def test_services_aggregates_docker_redis_pipeline(tmp_db, ts0):
    """services() rolls up the latest docker/redis/pipeline row per (node, entity) via
    MAX(ts), flags bad containers, surfaces a daemon-down node and keeps the hottest
    redis streams."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "docker_samples", [
        {"ts": ts0, "name": "edge", "image": "x", "state": "running", "running": 1,
         "health": "healthy", "exit_code": 0, "restart_count": 0, "oom_killed": 0,
         "cpu_pct": 9.0, "mem_mb": 80.0, "pids": 4},
        {"ts": ts0, "name": "watchtower", "image": "x", "state": "exited", "running": 0,
         "health": "", "exit_code": 1, "restart_count": 7, "oom_killed": 0,
         "cpu_pct": None, "mem_mb": None, "pids": None},
    ], node="app01")
    schema.insert(conn, "docker_samples", [{"ts": ts0, "name": "__daemon__", "running": 0}], node="pi01")
    schema.insert(conn, "redis_samples", [
        {"ts": ts0, "instance": "127.0.0.1:6379", "stream": "__server__", "connected": 1,
         "used_memory_mb": 15.0, "xlen": None, "pending": None, "connected_clients": 5,
         "blocked_clients": 0, "ops_per_sec": 200.0, "evicted_keys": 0, "rejected_connections": 0},
        {"ts": ts0, "instance": "127.0.0.1:6379", "stream": "scanner:stats", "connected": 1,
         "used_memory_mb": None, "xlen": 50, "pending": 2},
    ], node="app01")
    schema.insert(conn, "proc_watch", [
        {"ts": ts0, "label": "gst", "count": 0, "cpu_pct": 0.0, "rss_mb": 0.0,
         "uptime_s": 0.0, "restarts": 3}], node="pi01")
    schema.insert(conn, "stream_probes", [
        {"ts": ts0, "url": "rtsp://x/cam", "ok": 0, "latency_ms": None, "status": "timeout"}], node="pi01")
    conn.commit()
    svc = hubapi.services(conn, now=ts0 + 5)
    by_name = {(c["node"], c["name"]): c for c in svc["docker"]}
    assert by_name[("app01", "watchtower")]["bad"] is True
    assert by_name[("app01", "edge")]["bad"] is False
    assert "pi01" in svc["docker_down"]
    r = svc["redis"][0]
    assert r["ops_per_sec"] == 200.0 and r["connected_clients"] == 5
    assert r["streams"] and r["streams"][0]["stream"] == "scanner:stats"
    assert svc["procs"][0]["label"] == "gst" and svc["procs"][0]["count"] == 0
    assert svc["streams"][0]["ok"] == 0
    conn.close()


def test_ingest_rate_window_series_and_recent_rate():
    """ingest_rate() rolls the in-memory event buffer into a recent bytes/sec gauge value, a
    per-bucket series for the sparkline, window totals and the last-ingest ts. Events older than
    the window are ignored; only events inside rate_window_s feed the gauge rate."""
    now = 1_000_000.0
    events = [
        (now - 5, 1000, 4000, 10),     # inside rate window (60s) + series
        (now - 30, 2000, 8000, 20),    # inside rate window + series
        (now - 120, 500, 2000, 5),     # outside 60s rate window, inside 15m series window
        (now - 2000, 9999, 9999, 99),  # outside the 15m window -> fully ignored
    ]
    r = hubapi.ingest_rate(events, now=now, window_s=900.0, rate_window_s=60.0, buckets=60)
    assert r["posts"] == 3                       # the 2000s-old event is dropped
    assert r["total_wire_bytes"] == 3500         # 1000 + 2000 + 500
    assert r["total_rows"] == 35
    assert r["last_ts"] == now - 5
    assert r["bytes_per_s"] == round(3000 / 60.0, 1)   # only the two <=60s events
    assert r["rows_per_s"] == round(30 / 60.0, 3)
    assert len(r["series_bytes"]) == 60
    assert sum(r["series_bytes"]) == 3500


def test_ingest_rate_empty():
    r = hubapi.ingest_rate([], now=1000.0)
    assert r["posts"] == 0 and r["bytes_per_s"] == 0.0 and r["rows_per_s"] == 0.0
    assert r["last_ts"] is None and sum(r["series_bytes"]) == 0


def test_heatmap_grid(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    hm = hubapi.heatmap(conn, "loss", hours=24, until=ts0 + 100)
    assert hm["metric"] == "loss"
    assert set(hm["nodes"]) == {"app01", "pi01"}
    # pi01 had 100% loss in the window -> some bucket carries a high value.
    assert max(v for v in hm["nodes"]["pi01"] if v is not None) >= 100.0
    conn.close()


def test_risks_shape_and_anomalies(tmp_db, ts0):
    """risks() returns the new anomalies + incident_groups tiers; a node whose cpu/mem/temp
    co-deviate in the same bucket surfaces a multivariate anomaly."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    # 15 quiet host buckets, then one bucket where cpu/mem/temp all jump together. build_frame
    # buckets at 60s, so space samples one per minute and put the joint jump in the last.
    rows = []
    for i in range(16):
        spike = i == 15
        rows.append({
            "ts": ts0 + i * 60, "cpu_pct": 70.0 if spike else 20.0,
            "load1": 0.5, "load5": 0.5, "load15": 0.5,
            "mem_used_pct": 85.0 if spike else 40.0,
            "mem_total_mb": 4000.0, "temp_c": 75.0 if spike else 50.0})
    schema.insert(conn, "host_samples", rows, node="pi01")
    conn.commit()
    now = ts0 + 16 * 60
    out = hubapi.risks(conn, hours=24, now=now)
    assert set(out) >= {"clocks", "alerts", "incidents", "incident_groups", "anomalies"}
    assert isinstance(out["anomalies"], list) and isinstance(out["incident_groups"], list)
    an = [a for a in out["anomalies"] if a["node"] == "pi01"]
    assert an, "co-deviating cpu/mem/temp bucket should produce an anomaly"
    names = {n for n, _z in an[0]["signals"]}
    assert {"cpu", "mem", "temp"} <= names
    conn.close()
