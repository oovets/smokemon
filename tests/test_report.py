"""Text surfaces over the incident store: status line, incident table and digest, plus the
sparkline primitive. Seeds a small node DB and checks the rendered strings."""

from smokemon import query, report, schema

NODE = "pi01"


def test_sparkline_shapes():
    assert report.sparkline([]) == ""
    s = report.sparkline([0, 1, 2, 3, 4, 5, 6, 7])
    assert s[0] == "▁" and s[-1] == "█"
    # None renders as a gap (space).
    assert " " in report.sparkline([1.0, None, 2.0])
    # constant series is all the lowest block (no div-by-zero).
    assert report.sparkline([5, 5, 5]) == "▁▁▁"


def _seed(conn, ts0, *, close=True):
    """One rtt incident plus a heartbeat."""
    rows = [{"ts": ts0, "uid": "u1", "transition": "open", "signal": "rtt",
             "entity": "1.1.1.1", "severity": "crit", "value": 400.0, "opened_ts": ts0}]
    if close:
        rows.append({"ts": ts0 + 90, "uid": "u1", "transition": "close", "signal": "rtt",
                     "entity": "1.1.1.1", "severity": "info", "opened_ts": ts0,
                     "duration_s": 90.0, "worst_value": 512.0})
    schema.insert(conn, "incidents", rows, node=NODE)
    schema.insert(conn, "heartbeats", [{
        "ts": ts0 + 100, "interval_s": 300.0, "agent_uptime_s": 7200.0, "cpu_pct": 12.0,
        "temp_c": 58.0, "db_mb": 2.5, "wal_mb": 0.4, "disk_used_pct": 71.0,
        "disk_free_gb": 9.5, "wear_pct": 12.0, "signal_drops": 0}], node=NODE)
    conn.commit()


def test_status_line_reports_open_incident_and_heartbeat(node_db, ts0):
    _seed(node_db, ts0, close=False)
    line = report.status_line(node_db, ts0 - 10, ts0 + 310, NODE)
    assert "crit" in line and "rtt 1.1.1.1" in line
    assert "heartbeat" in line and "cpu 12%" in line and "58C" in line


def test_status_line_says_recovered_then_healthy(node_db, ts0):
    _seed(node_db, ts0)
    assert "recovered" in report.status_line(node_db, ts0 - 10, ts0 + 310, NODE)
    # A window with no incidents at all reads healthy, and still shows the heartbeat age -
    # silence without a heartbeat is indistinguishable from a dead node.
    line = report.status_line(node_db, ts0 + 200, ts0 + 310, NODE)
    assert "healthy" in line and "heartbeat" in line


def test_status_line_flags_a_node_that_never_reported(node_db, ts0):
    assert "no heartbeat" in report.status_line(node_db, ts0 - 10, ts0 + 10, "ghost")


def test_incidents_report_lists_incidents(node_db, ts0):
    _seed(node_db, ts0)
    out = report.incidents_report(node_db, ts0 - 10, ts0 + 310, NODE)
    assert "1 incident(s)" in out
    assert "crit" in out and "rtt 1.1.1.1" in out
    assert "1m30s" in out          # duration from the close row
    assert "worst 512.0" in out


def test_incidents_report_all_clear(node_db, ts0):
    assert "all clear" in report.incidents_report(node_db, ts0 - 60, ts0 + 60)


def test_digest_narrative(node_db, ts0):
    _seed(node_db, ts0)
    out = report.digest(node_db, ts0 - 10, ts0 + 310, NODE)
    assert "smokemon digest" in out
    assert "1 incident(s): 1 crit." in out
    assert "Time in incident: 1m30s" in out
    assert "Last heartbeat:" in out and "agent up 2h00m" in out
    assert "Disk: 71% used, 9.5 GB free." in out
    assert "SD wear: 12%." in out
    assert "Own database: 2.5 MB (+0.4 MB WAL)." in out
    assert "Top incidents (of 1):" in out


def test_digest_flags_dropped_signals(node_db, ts0):
    """A shed signal means the detector had incomplete coverage, so 'no incidents' would be
    a claim the data cannot support. It has to be said out loud."""
    schema.insert(node_db, "heartbeats",
                  [{"ts": ts0, "interval_s": 300.0, "signal_drops": 7}], node=NODE)
    node_db.commit()
    out = report.digest(node_db, ts0 - 10, ts0 + 10, NODE)
    assert "No incidents detected." in out
    assert "Detector dropped 7 signal(s)" in out


def test_digest_reports_an_ongoing_incident(node_db, ts0):
    _seed(node_db, ts0, close=False)
    out = report.digest(node_db, ts0 - 10, ts0 + 310, NODE)
    assert "Still open: rtt 1.1.1.1." in out
    # An open incident has no duration yet; the digest must say so rather than print 0s.
    assert "ongoing" in out
    assert query.load_incidents(node_db, ts0 - 10, ts0 + 310, NODE)[0]["state"] == "ongoing"
