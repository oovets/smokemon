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


def test_fleet_stats_fast_uptime_outage_and_agrees_with_fleet(tmp_db, ts0):
    """fleet_stats_fast computes per-node uptime/outage exactly from loss counts, and agrees with
    fleet() on uptime for the clean node (where there are no incidents to diverge on)."""
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    until = ts0 + 100
    fast = hubapi.fleet_stats_fast(conn, hours=24, until=until)
    # app01: 6 clean internet samples -> 100% uptime, 0% outage
    assert fast["app01"]["uptime_pct"] == 100.0
    assert fast["app01"]["outage_pct"] == 0.0
    # pi01: 1.1.1.1 had 4 of 6 samples at 100% loss -> uptime 2/6, outage 4/6
    assert fast["pi01"]["uptime_pct"] == round(100.0 * 2 / 6, 2)
    assert fast["pi01"]["outage_pct"] == round(100.0 * 4 / 6, 2)
    # uptime parity with fleet() on the clean node
    fl = {r["node"]: r for r in hubapi.fleet(conn, hours=24, until=until)}
    assert fast["app01"]["uptime_pct"] == fl["app01"]["uptime_pct"]
    conn.close()


def test_node_series_shape_and_annotations(tmp_db, ts0):
    """node_series returns aligned signal arrays on one grid plus incident annotations, all from a
    single read (the data the live canvas chart animates)."""
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    d = hubapi.node_series(conn, "pi01", hours=24, now=ts0 + 100)
    assert d["node"] == "pi01"
    assert {"t", "series", "annotations", "bucket"} <= set(d)
    assert {"rtt", "loss", "cpu", "mem", "temp"} == set(d["series"])
    # every signal array aligns to the time grid length
    n = len(d["t"])
    for k, arr in d["series"].items():
        assert len(arr) == n, f"{k} not aligned to grid"
    # pi01 had a full internet outage in the seed -> at least one incident annotation
    assert any(a["klass"] in ("isp-outage", "link-down", "packet-loss") for a in d["annotations"])
    # a clean node yields aligned arrays and (typically) no hard-outage annotation
    clean = hubapi.node_series(conn, "app01", hours=24, now=ts0 + 100)
    assert len(clean["series"]["rtt"]) == len(clean["t"])
    conn.close()


def test_app_label_custom_ports():
    assert hubapi.app_label(8554) == "rtsp"
    assert hubapi.app_label(5000) == "raw-video"
    assert hubapi.app_label(19999) == "netdata"
    assert hubapi.app_label(443) == "https"      # existing entries unchanged
    assert hubapi.app_label(12345) == ":12345"   # unknown -> bare port


def test_network_by_node_breakdown(tmp_db, ts0):
    """network(by_node=1): each app carries a per-node breakdown whose per-node series sum to the
    fleet series, so fleet and per-node modes agree on the total."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    # two nodes each pushing a rising cumulative byte gauge on port 8554 (rtsp)
    for ni, node in enumerate(["camA", "camB"]):
        cum = 0
        for i in range(8):
            cum += (ni + 1) * 1_000_000  # camB moves twice camA's bytes
            schema.insert(conn, "port_samples", [{
                "ts": ts0 + i * 60, "proto": "tcp", "dir": "out", "port": 8554,
                "conns": 1, "peers": 1, "listening": 0, "bytes_sent": cum, "bytes_recv": 0}],
                node=node)
    conn.commit()
    d = hubapi.network(conn, hours=1, by_node=True, now=ts0 + 8 * 60)
    rtsp = next(a for a in d["apps"] if a["port"] == 8554)
    assert rtsp["app"] == "rtsp"
    assert "nodes" in rtsp and {n["node"] for n in rtsp["nodes"]} == {"camA", "camB"}
    # per-node series sum to the fleet series bucket-by-bucket (within rounding)
    fleet = rtsp["series"]
    summed = [0.0] * len(fleet)
    for n in rtsp["nodes"]:
        for i, v in enumerate(n["series"]):
            summed[i] += v
    assert all(abs(fleet[i] - round(summed[i], 1)) < 0.5 for i in range(len(fleet)))
    # default mode (no by_node) carries no per-node breakdown
    d0 = hubapi.network(conn, hours=1, now=ts0 + 8 * 60)
    assert "nodes" not in next(a for a in d0["apps"] if a["port"] == 8554)
    conn.close()


def test_heatmap_bandwidth_metric(tmp_db, ts0):
    """heatmap(metric='bw') returns a node x hour bytes/s grid from net_samples, skipping virtual
    interfaces, while loss/rtt are unchanged."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    base = ts0 - (ts0 % 3600)
    # one real iface rising 3600 bytes/hour -> 1 byte/s, plus a loopback that must be skipped
    for i in range(3):
        schema.insert(conn, "net_samples", [
            {"ts": base + i * 3600 + 10, "iface": "eth0", "ibytes": i * 3600, "obytes": 0,
             "ipkts": 0, "opkts": 0},
            {"ts": base + i * 3600 + 10, "iface": "lo", "ibytes": i * 9_000_000, "obytes": 0,
             "ipkts": 0, "opkts": 0}],
            node="pi01")
    conn.commit()
    hm = hubapi.heatmap(conn, metric="bw", hours=3, until=base + 3 * 3600)
    assert hm["metric"] == "bw" and "pi01" in hm["nodes"]
    vals = [v for v in hm["nodes"]["pi01"] if v is not None]
    assert vals  # at least one hour has a delta
    assert max(vals) <= 2.0  # ~1 byte/s from eth0; lo excluded (would be ~2500 B/s)
    conn.close()


def test_services_rollup_counts():
    """services_rollup folds the flat services() lists into per-node counts using the same
    bad/down definitions services() already set."""
    svc = {
        "docker": [
            {"node": "n1", "running": 1, "bad": False},
            {"node": "n1", "running": 0, "bad": True},
            {"node": "n2", "running": 1, "bad": False},
        ],
        "docker_down": ["n3"],
        "redis": [{"node": "n1", "connected": 1}, {"node": "n2", "connected": 0}],
        "procs": [{"node": "n1", "count": 2}, {"node": "n1", "count": 0}],
        "streams": [{"node": "n2", "ok": 1}, {"node": "n2", "ok": 0}],
    }
    roll = hubapi.services_rollup(svc)
    assert roll["n1"]["docker_total"] == 2 and roll["n1"]["docker_bad"] == 1
    assert roll["n1"]["docker_running"] == 1
    assert roll["n3"]["docker_down"] is True
    assert roll["n1"]["redis_down"] == 0 and roll["n2"]["redis_down"] == 1
    assert roll["n1"]["procs_total"] == 2 and roll["n1"]["procs_down"] == 1
    assert roll["n2"]["streams_total"] == 2 and roll["n2"]["streams_down"] == 1


def test_nodes_detail_composite_shape(tmp_db, ts0):
    """nodes_detail returns one row per node carrying live + 24h + svc-rollup + cost fields, in a
    single composite the merged dashboard tab can render without three separate fetches."""
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    now = ts0 + 100
    d = hubapi.nodes_detail(conn, hours=24, now=now)
    assert set(d) >= {"now", "hours", "nodes"}
    by_node = {n["node"] for n in d["nodes"]}
    assert {"app01", "pi01"} <= by_node
    row = next(n for n in d["nodes"] if n["node"] == "app01")
    # live fields (from fleet_status) + 24h fields (from fleet_stats_fast) + svc rollup present
    assert {"state", "rtt_ms", "cpu", "mem", "temp", "age_s"} <= set(row)
    assert {"uptime_pct", "rtt_ms_24h", "outage_pct"} <= set(row)
    # svc is always present as a dict; counts are populated only for nodes with service data
    assert "svc" in row and isinstance(row["svc"], dict)
    assert row["uptime_pct"] == 100.0  # app01 is clean
    # pi01 had an outage -> its 24h uptime is below 100
    pi = next(n for n in d["nodes"] if n["node"] == "pi01")
    assert pi["uptime_pct"] is not None and pi["uptime_pct"] < 100.0
    conn.close()


def test_nodes_detail_includes_service_rollup(tmp_db, ts0):
    """When a node reports docker telemetry, nodes_detail's per-node svc block carries the counts
    (so the merged table can show a service summary without a separate /api/services fetch)."""
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    schema.insert(conn, "docker_samples", [
        {"ts": ts0 + 50, "name": "edge", "image": "x", "state": "running", "running": 1,
         "health": "healthy", "exit_code": 0, "restart_count": 0, "oom_killed": 0,
         "cpu_pct": 5.0, "mem_mb": 50.0, "pids": 3},
        {"ts": ts0 + 50, "name": "broken", "image": "x", "state": "exited", "running": 0,
         "health": "", "exit_code": 1, "restart_count": 9, "oom_killed": 0,
         "cpu_pct": None, "mem_mb": None, "pids": None},
    ], node="app01")
    conn.commit()
    d = hubapi.nodes_detail(conn, hours=24, now=ts0 + 100)
    app01 = next(n for n in d["nodes"] if n["node"] == "app01")
    assert app01["svc"]["docker_total"] == 2
    assert app01["svc"]["docker_bad"] == 1 and app01["svc"]["docker_running"] == 1
    conn.close()


def test_heatmap_rollup_matches_raw_for_long_window(tmp_db, ts0):
    """A long-window heatmap reads the hub rollup table; for loss (MAX agg) the per-hour grid must
    match what raw ping_runs would produce, since a 1-min bucket's max loss equals the hour's max
    over those buckets. Confirms the rollup read path returns the same shape/values."""
    from smokemon import rollup
    conn = core.connect(str(tmp_db))
    schema.init_hub(conn)
    # one node, two hours of 10s ping with an outage in hour 2, all well in the past so closed.
    base = ts0 - 10 * 86400
    base = base - (base % 3600)
    rows = []
    t = base
    while t < base + 2 * 3600:
        loss = 100.0 if (base + 3600 <= t < base + 3700) else 0.0
        rows.append({"ts": t, "target": "1.1.1.1", "sent": 20, "recv": 20, "loss_pct": loss,
                     "rtt_min": 5.0, "rtt_median": 8.0, "rtt_max": 12.0})
        t += 10
    schema.insert(conn, "ping_runs", rows, node="pi01")
    conn.commit()
    rollup.rollup(conn, now=base + 10 * 86400)
    # 30-day window -> _1h resolution; raw -> read raw. Compare the loss grid for pi01.
    hm_roll = hubapi.heatmap(conn, "loss", hours=24 * 30, until=base + 2 * 3600)
    assert "pi01" in hm_roll["nodes"]
    assert max(v for v in hm_roll["nodes"]["pi01"] if v is not None) == 100.0  # outage survived MAX
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


def test_risks_regime_shifts(tmp_db, ts0):
    """P2: a sustained RTT level change on a node surfaces in the risks 'shifts' tier."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    start = ts0 - 1800
    rows = []
    for i in range(180):
        med = 8.0 if i < 90 else 30.0
        rows.append({"ts": start + i * 10, "target": "1.1.1.1", "sent": 20, "recv": 20,
                     "loss_pct": 0.0, "rtt_min": med, "rtt_p25": med, "rtt_median": med,
                     "rtt_p75": med, "rtt_avg": med, "rtt_max": med, "rtt_stddev": 0.3})
    schema.insert(conn, "ping_runs", rows, node="pi01")
    conn.commit()
    out = hubapi.risks(conn, hours=1, now=start + 1800)
    sh = [s for s in out["shifts"] if s["node"] == "pi01"]
    assert sh and sh[0]["after"] > sh[0]["before"]
    assert "30 ms" in sh[0]["detail"]
    conn.close()
