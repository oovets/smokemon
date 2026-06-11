"""F1 correlation, F2 incident detection, P1 anomaly baseline, P2 change-points,
P3 mtr path intelligence, X5 bandwidth attribution - smokemon.analyze.

Pure helpers are exercised directly; the DB-backed analyses (path, attribution,
new_processes, explain_incident) run against a temp node DB. All stdlib."""

import math

from smokemon import analyze, core, schema

# ---------- pure stats ----------

def test_resample_buckets_and_aggregates():
    # three points in bucket 0 (mean), one in bucket 2, bucket 1 empty -> None.
    t = [0.0, 1.0, 2.0, 20.0]
    v = [1.0, 2.0, 3.0, 9.0]
    grid, out = analyze.resample(t, v, 0.0, 30.0, 10.0)
    assert grid[0] == 0.0
    assert out[0] == 2.0       # mean of 1,2,3
    assert out[1] is None      # empty bucket
    assert out[2] == 9.0
    # max agg
    _, mx = analyze.resample(t, v, 0.0, 30.0, 10.0, "max")
    assert mx[0] == 3.0


def test_robust_z_and_constant_baseline():
    assert analyze.robust_z(10.0, 10.0, 2.0) == 0.0
    assert analyze.robust_z(20.0, 10.0, 0.0) == 50.0     # constant baseline, value above
    assert analyze.robust_z(5.0, 10.0, 0.0) == -50.0
    z = analyze.robust_z(14.826, 0.0, 1.0)
    assert math.isclose(z, 10.0, rel_tol=1e-3)


def test_pearson():
    assert math.isclose(analyze.pearson([1, 2, 3], [2, 4, 6]), 1.0, rel_tol=1e-9)
    assert math.isclose(analyze.pearson([1, 2, 3], [6, 4, 2]), -1.0, rel_tol=1e-9)
    assert analyze.pearson([1, 1, 1], [1, 2, 3]) is None    # constant
    assert analyze.pearson([1, 2], [1, 2]) is None          # too few


def test_runs():
    assert list(analyze._runs([False, True, True, False, True])) == [(1, 2), (4, 4)]
    assert list(analyze._runs([])) == []
    assert list(analyze._runs([True, True])) == [(0, 1)]


# ---------- target classification ----------

def test_classify_target():
    assert analyze.classify_target("192.168.0.1") == "gw"
    assert analyze.classify_target("10.0.0.1") == "gw"
    assert analyze.classify_target("100.100.100.100") == "tailscale"
    assert analyze.classify_target("1.1.1.1") == "internet"
    # label-driven (config maps 192.168.0.1 -> gw, 1.1.1.1 -> internet)
    assert analyze.classify_target("1.1.1.1") == "internet"


# ---------- F2 incidents ----------

def _ping(t0, loss, med, target="1.1.1.1"):
    n = len(loss)
    return {target: {"t": [t0 + i * 10 for i in range(n)], "loss": loss,
                     "med": med, "min": [m - 1 for m in med], "max": [m + 1 for m in med]}}


def test_detect_isp_outage_vs_link_down():
    t0 = 1_000_000.0
    # internet target 100% loss for 3 cycles, gateway clean -> isp-outage.
    data = {
        "1.1.1.1": {"t": [t0, t0 + 10, t0 + 20], "loss": [100.0, 100.0, 100.0],
                    "med": [None, None, None], "min": [None] * 3, "max": [None] * 3},
        "192.168.0.1": {"t": [t0, t0 + 10, t0 + 20], "loss": [0.0, 0.0, 0.0],
                        "med": [1.0, 1.0, 1.0], "min": [0.5] * 3, "max": [1.5] * 3},
    }
    incs = analyze.detect_incidents(data)
    klasses = {i["klass"] for i in incs}
    assert "isp-outage" in klasses
    assert "link-down" not in klasses


def test_detect_link_down_when_gateway_also_lost():
    t0 = 1_000_000.0
    data = {
        "1.1.1.1": {"t": [t0, t0 + 10, t0 + 20], "loss": [100.0, 100.0, 100.0],
                    "med": [None] * 3, "min": [None] * 3, "max": [None] * 3},
        "192.168.0.1": {"t": [t0, t0 + 10, t0 + 20], "loss": [100.0, 100.0, 100.0],
                        "med": [None] * 3, "min": [None] * 3, "max": [None] * 3},
    }
    klasses = {i["klass"] for i in analyze.detect_incidents(data)}
    assert "link-down" in klasses


def test_detect_latency_spike():
    t0 = 1_000_000.0
    # baseline ~10ms then a sustained 3x spike.
    med = [10.0] * 6 + [200.0, 220.0, 210.0]
    data = _ping(t0, [0.0] * 9, med)
    incs = [i for i in analyze.detect_incidents(data) if i["klass"] == "latency-spike"]
    assert incs and incs[0]["severity"] >= 1


def test_detect_dns_slow():
    t0 = 1_000_000.0
    http = {"https://x.com": {
        "t": [t0, t0 + 60, t0 + 120],
        "dns": [200.0, 220.0, 210.0], "connect": [5.0, 6.0, 5.0],
        "tls": [10.0, 11.0, 10.0], "ttfb": [230.0, 250.0, 240.0]}}
    incs = [i for i in analyze.detect_incidents({}, http) if i["klass"] == "dns-slow"]
    assert incs


# ---------- F1 correlation ----------

def test_explain_incident_flags_cpu_and_temp():
    bucket = 60.0
    t0 = 1_000_000.0
    grid = [t0 + i * bucket for i in range(10)]
    # baseline cpu ~20, spikes to 99 in buckets 7-8 (the incident window).
    cpu = [20.0, 22.0, 19.0, 21.0, 20.0, 18.0, 22.0, 99.0, 98.0, 20.0]
    temp = [40.0] * 7 + [75.0, 76.0, 41.0]
    frame = {"t": grid, "bucket": bucket, "series": {"cpu": cpu, "temp": temp}}
    causes = analyze.explain_incident(frame, grid[7], grid[8])
    joined = " ".join(causes)
    assert "cpu" in joined and "temp" in joined


# ---------- P1 anomaly baseline ----------

def test_tod_baseline_and_anomaly():
    # 9 Tuesday-14:00 samples around 10ms + one outlier at 100ms.
    import datetime as _dt
    tue14 = _dt.datetime(2026, 5, 26, 14, 0, 0)  # a Tuesday
    base_t = [(tue14 + _dt.timedelta(weeks=w)).timestamp() for w in range(9)]
    base_v = [10.0, 11.0, 9.0, 10.5, 9.5, 10.0, 11.0, 9.0, 10.0]
    base = analyze.tod_baseline(base_t, base_v)
    assert (1, 14) in base  # weekday 1 = Tuesday
    anoms = analyze.tod_anomalies([base_t[0]], [100.0], z_thresh=4.0, baseline=base)
    assert anoms and anoms[0]["value"] == 100.0


# ---------- P2 change points ----------

def test_change_point_detects_regime_shift():
    t = list(range(40))
    vals = [940.0] * 20 + [230.0] * 20    # bandwidth tier drop
    cps = analyze.change_points(t, vals, min_seg=5)
    assert cps
    cp = cps[0]
    assert 18 <= cp["ts"] <= 22
    assert cp["before"] > cp["after"]


def test_change_point_none_when_stable():
    t = list(range(40))
    vals = [500.0 + (i % 3) for i in range(40)]   # tiny noise only
    assert analyze.change_points(t, vals, min_seg=5) == []


# ---------- DB-backed: P3 path, X5 attribution, new processes ----------

def test_path_analysis_detects_route_change(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    # hop 2 changes host between the two samples -> a route change; hop 1 stable.
    rows = [
        {"ts": ts0, "target": "1.1.1.1", "hop_no": 1, "host": "gw", "loss_pct": 0.0,
         "sent": 10, "last_ms": 1.0, "avg_ms": 1.0, "best_ms": 1.0, "worst_ms": 2.0, "stddev_ms": 0.1},
        {"ts": ts0, "target": "1.1.1.1", "hop_no": 2, "host": "isp-a", "loss_pct": 0.0,
         "sent": 10, "last_ms": 5.0, "avg_ms": 5.0, "best_ms": 4.0, "worst_ms": 6.0, "stddev_ms": 0.5},
        {"ts": ts0 + 60, "target": "1.1.1.1", "hop_no": 1, "host": "gw", "loss_pct": 0.0,
         "sent": 10, "last_ms": 1.0, "avg_ms": 1.0, "best_ms": 1.0, "worst_ms": 2.0, "stddev_ms": 0.1},
        {"ts": ts0 + 60, "target": "1.1.1.1", "hop_no": 2, "host": "isp-b", "loss_pct": 30.0,
         "sent": 10, "last_ms": 9.0, "avg_ms": 9.0, "best_ms": 8.0, "worst_ms": 12.0, "stddev_ms": 1.0},
    ]
    schema.insert(conn, "mtr_hops", rows)
    conn.commit()
    pa = analyze.path_analysis(conn, ts0 - 60, ts0 + 120)
    p = pa["1.1.1.1"]
    assert p["hops"] == 2
    assert any(c["hop_no"] == 2 and c["from"] == "isp-a" and c["to"] == "isp-b"
               for c in p["route_changes"])
    assert p["worst_hop"]["hop_no"] == 2   # highest loss
    assert math.isclose(p["stability"], 0.5, rel_tol=1e-9)
    conn.close()


def test_new_processes(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "proc_samples", [
        {"ts": ts0 - 1000, "pid": 1, "name": "always", "cpu_pct": 1.0, "rss_mb": 10.0},
        {"ts": ts0 + 5, "pid": 1, "name": "always", "cpu_pct": 1.0, "rss_mb": 10.0},
        {"ts": ts0 + 5, "pid": 2, "name": "backup", "cpu_pct": 90.0, "rss_mb": 50.0},
    ])
    conn.commit()
    procs = analyze.new_processes(conn, ts0, ts0 + 60)
    assert procs == ["backup"]
    conn.close()


def test_bandwidth_attribution(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    # flat 1 Mb/s baseline then a big download spike; a hungry proc during the spike.
    base = []
    ib = 0
    for i in range(12):
        ib += int(1e6 / 8 * 60)              # ~1 Mbit/s for 60s
        base.append({"ts": ts0 + i * 60, "iface": "en0", "ibytes": ib,
                     "obytes": 0, "ipkts": 0, "opkts": 0})
    ib += int(500e6 / 8 * 60)                # ~500 Mbit/s spike in the next 60s
    base.append({"ts": ts0 + 12 * 60, "iface": "en0", "ibytes": ib,
                 "obytes": 0, "ipkts": 0, "opkts": 0})
    schema.insert(conn, "net_samples", base)
    schema.insert(conn, "proc_samples", [
        {"ts": ts0 + 12 * 60 + 5, "pid": 9, "name": "bittorrent", "cpu_pct": 80.0, "rss_mb": 100.0}])
    conn.commit()
    attrib = analyze.bandwidth_attribution(conn, ts0 - 60, ts0 + 14 * 60, bucket=60.0)
    assert attrib
    assert attrib[0]["direction"] == "down"
    assert "bittorrent" in attrib[0]["procs"]
    conn.close()


# ---------- F3: incident correlation / storm dedup ----------

def test_correlate_incidents_groups_overlapping():
    """Three incidents firing in the same window collapse into one group whose root is the
    highest-severity member; all three are kept as members."""
    incs = [
        {"start": 100.0, "end": 160.0, "severity": 1, "klass": "latency-spike"},
        {"start": 120.0, "end": 200.0, "severity": 3, "klass": "isp-outage"},
        {"start": 210.0, "end": 240.0, "severity": 2, "klass": "packet-loss"},
    ]
    groups = analyze.correlate_incidents(incs, window_s=120.0)
    assert len(groups) == 1
    g = groups[0]
    assert g["start"] == 100.0 and g["end"] == 240.0
    assert g["severity"] == 3
    assert g["root"]["klass"] == "isp-outage"
    assert len(g["members"]) == 3


def test_correlate_incidents_splits_distant():
    """Incidents separated by more than window_s stay in their own groups (a genuine second
    fault is not folded into the first)."""
    incs = [
        {"start": 100.0, "end": 130.0, "severity": 2, "klass": "packet-loss"},
        {"start": 1000.0, "end": 1030.0, "severity": 2, "klass": "latency-spike"},
    ]
    groups = analyze.correlate_incidents(incs, window_s=120.0)
    assert len(groups) == 2
    assert [len(g["members"]) for g in groups] == [1, 1]


def test_correlate_incidents_empty():
    assert analyze.correlate_incidents([]) == []


def test_explain_incident_pearson_annotation():
    """A suspect whose series moves with the impact (rtt) series in-window gets an r= tag."""
    n = 10
    frame = {
        "t": [i * 60.0 for i in range(n)], "bucket": 60.0,
        "series": {
            # rtt rises through the incident window (last 5 buckets)
            "rtt": [10.0] * 5 + [100.0, 110.0, 120.0, 130.0, 140.0],
            # cpu rises in lockstep -> pearson r = +1.0 against rtt
            "cpu": [20.0] * 5 + [50.0, 60.0, 70.0, 80.0, 90.0],
        },
    }
    causes = analyze.explain_incident(frame, 300.0, 540.0)
    assert causes and causes[0].startswith("cpu")
    assert "r=+1.00" in causes[0]
