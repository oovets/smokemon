"""`smoke fleet` text renderers: the terminal twin of the web dashboard. Checks the
pure-stdlib report functions against both crafted payloads (the /api shapes) and a
real two-node hub DB run through hubapi -> report."""

from smokemon import core, hubapi, report, schema


def _ping_rows(ts0, target, losses, med):
    rows = []
    for i, loss in enumerate(losses):
        m = None if loss >= 100 else med
        rows.append({"ts": ts0 + i * 10, "target": target, "sent": 20,
                     "recv": 0 if loss >= 100 else 20, "loss_pct": loss,
                     "rtt_min": m, "rtt_p25": m, "rtt_median": m, "rtt_p75": m,
                     "rtt_avg": m, "rtt_max": m, "rtt_stddev": 0.1})
    return rows


def test_fleet_status_report_renders_states_and_counts():
    status = {
        "counts": {"healthy": 1, "warn": 1, "down": 1, "stale": 1},
        "nodes": [
            {"node": "down01", "state": "down", "rtt_ms": 9.0, "loss_pct": 100.0,
             "cpu": 80.0, "temp": 70.0, "age_s": 5},
            {"node": "stale01", "state": "stale", "rtt_ms": None, "loss_pct": None,
             "cpu": None, "temp": None, "age_s": 1200},
            {"node": "warn01", "state": "warn", "rtt_ms": 300.0, "loss_pct": 0.0,
             "cpu": 50.0, "temp": None, "age_s": 4},
            {"node": "app01", "state": "healthy", "rtt_ms": 8.0, "loss_pct": 0.0,
             "cpu": 22.0, "temp": 46.0, "age_s": 3},
        ],
    }
    out = report.fleet_status_report(status, color=False)
    assert "FLEET — 4 node(s)" in out
    assert "1 healthy" in out and "1 down" in out and "1 stale" in out
    # one line per node, healthy line carries rtt + cpu + temp
    assert "app01" in out and "8ms" in out and "cpu22%" in out and "46C" in out
    # down node surfaces loss, stale node surfaces relative age, not rtt
    assert "loss100%" in out
    assert "20m ago" in out
    # color=False must emit no ANSI escapes
    assert "\x1b[" not in out


def test_fleet_status_report_colors_when_enabled():
    status = {"counts": {"healthy": 1, "warn": 0, "down": 0, "stale": 0},
              "nodes": [{"node": "a", "state": "healthy", "rtt_ms": 8.0,
                         "loss_pct": 0.0, "cpu": 10.0, "temp": 40.0, "age_s": 2}]}
    assert "\x1b[32m" in report.fleet_status_report(status, color=True)


def test_fleet_status_report_empty():
    out = report.fleet_status_report({"counts": {}, "nodes": []}, color=False)
    assert "0 node(s)" in out and "no nodes reporting" in out


def test_fleet_ranked_report_worst_first_and_downtime():
    fleet = [
        {"node": "pi01", "uptime_pct": 80.0, "rtt_ms": 9.0, "incidents": 2, "downtime_s": 90.0},
        {"node": "app01", "uptime_pct": 100.0, "rtt_ms": 8.0, "incidents": 0, "downtime_s": 0.0},
    ]
    out = report.fleet_ranked_report(fleet, hours=24, color=False)
    assert "last 24h · 2 node(s)" in out
    lines = out.splitlines()
    # the degraded node must appear before the healthy one (worst-first preserved)
    assert lines.index([x for x in lines if "pi01" in x][0]) \
        < lines.index([x for x in lines if "app01" in x][0])
    assert "80.0%" in out and "100.0%" in out
    assert "\x1b[" not in out


def test_fleet_heatmap_report_renders_rows_and_axis():
    hm = {"metric": "loss",
          "hours": [1_700_000_000 + i * 3600 for i in range(4)],
          "nodes": {"app01": [0.0, 0.0, 0.0, 0.0],
                    "pi01": [0.0, 100.0, 50.0, 0.0]}}
    out = report.fleet_heatmap_report(hm, color=False)
    assert "FLEET HEATMAP — loss%" in out
    # one row per node, worst (pi01) first
    assert out.index("pi01") < out.index("app01")
    # gaps render as spaces; no ANSI when color is off
    assert "\x1b[" not in out
    # a time axis line with HH:MM labels is present
    assert ":" in out.splitlines()[-1]


def test_fleet_heatmap_colors_severity():
    hm = {"metric": "loss", "hours": [1_700_000_000, 1_700_003_600],
          "nodes": {"pi01": [0.0, 100.0]}}
    colored = report.fleet_heatmap_report(hm, color=True)
    assert "\x1b[31m" in colored   # the 100%-loss cell is red
    assert "\x1b[32m" in colored   # the 0%-loss cell is green


def test_fleet_heatmap_empty():
    out = report.fleet_heatmap_report({"metric": "rtt", "hours": [], "nodes": {}}, color=False)
    assert "no nodes reporting" in out


def test_fleet_end_to_end_from_hub_db(tmp_db, ts0):
    """hubapi -> report, the same path `smoke fleet` (DB mode) takes."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "ping_runs",
                  _ping_rows(ts0, "1.1.1.1", [0.0, 0.0, 0.0], 8.0), node="app01")
    schema.insert(conn, "ping_runs",
                  _ping_rows(ts0, "1.1.1.1", [0.0, 0.0, 100.0], 9.0), node="pi01")
    conn.commit()
    status = hubapi.fleet_status(conn, stale_after_s=300.0, now=ts0 + 30)
    out = report.fleet_status_report(status, color=False)
    assert "app01" in out and "pi01" in out
    # pi01's latest sample is 100% loss -> down, sorted ahead of healthy app01
    assert out.index("pi01") < out.index("app01")

    ranked = hubapi.fleet(conn, hours=24, until=ts0 + 100)
    rout = report.fleet_ranked_report(ranked, hours=24, color=False)
    assert "pi01" in rout and "app01" in rout
    conn.close()
