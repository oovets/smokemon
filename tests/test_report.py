"""QW3 status line, F2/F1 incident report and F3 digest text surfaces, plus the
sparkline primitive. Seeds a small node DB and checks the rendered strings."""

from smokemon import core, report, schema


def test_sparkline_shapes():
    assert report.sparkline([]) == ""
    s = report.sparkline([0, 1, 2, 3, 4, 5, 6, 7])
    assert s[0] == "▁" and s[-1] == "█"
    # None renders as a gap (space).
    assert " " in report.sparkline([1.0, None, 2.0])
    # constant series is all the lowest block (no div-by-zero).
    assert report.sparkline([5, 5, 5]) == "▁▁▁"


def _seed(conn, ts0):
    schema.init_node(conn)
    # internet target: clean for a while, then a 100% loss run (gateway stays clean).
    n = 30
    runs = []
    rtts = []
    for i in range(n):
        loss = 100.0 if 20 <= i < 24 else 0.0
        med = None if loss else 8.0 + (i % 3)
        runs.append({"ts": ts0 + i * 10, "target": "1.1.1.1", "sent": 20,
                     "recv": 0 if loss else 20, "loss_pct": loss, "rtt_min": med,
                     "rtt_p25": med, "rtt_median": med, "rtt_p75": med, "rtt_avg": med,
                     "rtt_max": med, "rtt_stddev": 0.5})
        rtts.append({"ts": ts0 + i * 10, "target": "192.168.0.1", "sent": 20, "recv": 20,
                     "loss_pct": 0.0, "rtt_min": 1.0, "rtt_p25": 1.0, "rtt_median": 1.0,
                     "rtt_p75": 1.0, "rtt_avg": 1.0, "rtt_max": 2.0, "rtt_stddev": 0.1})
    schema.insert(conn, "ping_runs", runs + rtts)
    # host: a cpu/temp spike coincident with the loss window.
    host = []
    for i in range(n):
        spike = 20 <= i < 24
        host.append({"ts": ts0 + i * 10, "cpu_pct": 99.0 if spike else 20.0,
                     "load1": 1.0, "load5": 1.0, "load15": 1.0,
                     "mem_used_pct": 40.0, "mem_total_mb": 4000.0,
                     "temp_c": 78.0 if spike else 45.0})
    schema.insert(conn, "host_samples", host)
    schema.insert(conn, "proc_samples", [
        {"ts": ts0 + 205, "pid": 7, "name": "backup", "cpu_pct": 95.0, "rss_mb": 80.0}])
    conn.commit()


def test_status_line_reports_health_and_sparklines(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    line = report.status_line(conn, ts0 - 10, ts0 + 310)
    assert "internet" in line and "cpu" in line
    # the outage ended ~80s before the window end (within the 5-min recency), so the
    # verdict reflects the still-recent ISP outage.
    assert "ISP OUTAGE" in line
    conn.close()


def test_incidents_report_lists_and_blames(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    out = report.incidents_report(conn, ts0 - 10, ts0 + 310)
    assert "incident" in out.lower()
    assert "isp-outage" in out          # internet down, gw clean
    assert "correlates with" in out     # F1 blame line present
    assert "cpu" in out and "backup" in out
    conn.close()


def test_incidents_report_all_clear(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "ping_runs", [{
        "ts": ts0, "target": "1.1.1.1", "sent": 20, "recv": 20, "loss_pct": 0.0,
        "rtt_min": 8.0, "rtt_p25": 8.0, "rtt_median": 8.0, "rtt_p75": 8.0,
        "rtt_avg": 8.0, "rtt_max": 9.0, "rtt_stddev": 0.2}])
    conn.commit()
    out = report.incidents_report(conn, ts0 - 60, ts0 + 60)
    assert "all clear" in out
    conn.close()


def test_digest_narrative(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    out = report.digest(conn, ts0 - 10, ts0 + 310)
    assert "smokemon digest" in out
    assert "Uptime:" in out
    assert "incident(s):" in out
    assert "isp-outage" in out          # full-outage class in the breakdown
    assert "Hard downtime:" in out      # outage spans merged + reported
    assert "Thermals:" in out
    conn.close()


# ---------- P2/P3/X5/P1 digest + incident surfaces ----------


def _ping_row(ts, target, med, loss=0.0):
    recv = 0 if loss >= 100.0 else 20
    m = None if loss >= 100.0 else med
    return {"ts": ts, "target": target, "sent": 20, "recv": recv, "loss_pct": loss,
            "rtt_min": m, "rtt_p25": m, "rtt_median": m, "rtt_p75": m,
            "rtt_avg": m, "rtt_max": m, "rtt_stddev": 0.3}


def test_digest_rtt_shift_and_paths(tmp_db, ts0):
    """P2: a sustained 8->30 ms level change makes an 'RTT shift' digest line; P3: an
    mtr hop whose host changed makes a 'Paths:' line."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    start = ts0 - 1800
    rows = [_ping_row(start + i * 10, "1.1.1.1", 8.0 if i < 90 else 30.0)
            for i in range(180)]
    schema.insert(conn, "ping_runs", rows)
    schema.insert(conn, "mtr_hops", [
        {"ts": start + 60, "target": "1.1.1.1", "hop_no": 3, "host": "hop-a.example",
         "loss_pct": 0.0, "sent": 10, "avg_ms": 5.0},
        {"ts": start + 960, "target": "1.1.1.1", "hop_no": 3, "host": "hop-b.example",
         "loss_pct": 0.0, "sent": 10, "avg_ms": 5.0}])
    conn.commit()
    out = report.digest(conn, start - 10, start + 1810)
    assert "RTT shift:" in out and "→ 30 ms" in out
    assert "Paths:" in out and "route changed x1" in out
    conn.close()


def test_digest_bandwidth_spike_names_procs(tmp_db, ts0):
    """X5: a bandwidth spike makes a digest line that names the busiest coincident process."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    start = ts0 - 16 * 60
    # cumulative gauge: ~1 Mb/s baseline, one minute at ~100 Mb/s
    rows, gauge = [], 0
    for i in range(16):
        gauge += 750_000_000 if i == 10 else 7_500_000
        rows.append({"ts": start + i * 60, "iface": "eth0", "ibytes": gauge, "obytes": 0,
                     "ipkts": 0, "opkts": 0})
    schema.insert(conn, "net_samples", rows)
    schema.insert(conn, "proc_samples", [
        {"ts": start + 10 * 60 + 15, "pid": 9, "name": "rsync", "cpu_pct": 80.0, "rss_mb": 50.0}])
    conn.commit()
    out = report.digest(conn, start - 10, start + 16 * 60 + 10)
    assert "Bandwidth spikes:" in out and "down" in out
    assert "busiest: rsync" in out
    conn.close()


def test_digest_tod_anomaly(tmp_db):
    """P1: window RTT far above the same weekday+hour last week makes an 'Unusual for the
    hour' line. Timestamps are hour-aligned so window and baseline share a tod bucket."""
    import time as _time
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    now = _time.time()
    mid = now - (now % 3600) - 86400 + 1800        # middle of an hour, yesterday
    week = 7 * 86400
    # baseline: same weekday+hour one week earlier, comfortably inside [until-7d, since]
    hist = [_ping_row(mid - week + 600 + k * 10, "1.1.1.1", 8.0) for k in range(6)]
    win = [_ping_row(mid + k * 10, "1.1.1.1", 30.0) for k in range(25)]
    schema.insert(conn, "ping_runs", hist + win)
    conn.commit()
    out = report.digest(conn, mid - 300, mid + 300)
    assert "Unusual for the hour:" in out and "~8 ms typical" in out
    conn.close()


def test_incidents_report_path_footer(tmp_db, ts0):
    """P3: when mtr saw the route change during an incident window, the report says so."""
    conn = core.connect(str(tmp_db))
    _seed(conn, ts0)
    schema.insert(conn, "mtr_hops", [
        {"ts": ts0 + 60, "target": "1.1.1.1", "hop_no": 4, "host": "old.example",
         "loss_pct": 2.0, "sent": 10, "avg_ms": 12.0},
        {"ts": ts0 + 240, "target": "1.1.1.1", "hop_no": 4, "host": "new.example",
         "loss_pct": 2.0, "sent": 10, "avg_ms": 12.0}])
    conn.commit()
    out = report.incidents_report(conn, ts0 - 10, ts0 + 310)
    assert "path: 1.1.1.1 route changed x1" in out
    conn.close()
