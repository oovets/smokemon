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


def test_heatmap_grid(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    hm = hubapi.heatmap(conn, "loss", hours=24, until=ts0 + 100)
    assert hm["metric"] == "loss"
    assert set(hm["nodes"]) == {"app01", "pi01"}
    # pi01 had 100% loss in the window -> some bucket carries a high value.
    assert max(v for v in hm["nodes"]["pi01"] if v is not None) >= 100.0
    conn.close()
