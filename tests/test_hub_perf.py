"""Hub query performance guards.

These exist because a large hub DB used to make dashboard GETs time out (NetworkError). The
incident schema is far smaller than the old sample tables, but the read patterns are the same
shape -- cross-node `WHERE ts >= ?` windows and per-node scans -- so the indexes still have to
be there and still have to be chosen.
"""

from smokemon import hubapi, schema


def _plan(conn, sql, params=()):
    return " ".join(str(r[-1]) for r in
                    conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())


def test_hub_tables_have_perf_indexes(hub_conn):
    for t in schema.STD_TABLES:
        idx = {r[0] for r in hub_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (t,))}
        assert f"ix_{t}_ts" in idx, f"{t} is missing the plain (ts) index on the hub"
        assert f"ix_{t}_node_ts" in idx, f"{t} is missing the (node, ts) index on the hub"


def test_ts_only_window_seeks_the_index(hub_conn):
    """The cross-node windows behind the incident feed and the events log must SEARCH the (ts)
    index, not SCAN the table."""
    text = _plan(hub_conn, "SELECT COUNT(*) FROM incidents WHERE ts >= ?", (0.0,))
    assert "ix_incidents_ts" in text, text


def test_per_node_window_seeks_the_node_ts_index(hub_conn):
    text = _plan(hub_conn,
                 "SELECT * FROM incidents WHERE ts BETWEEN ? AND ? AND node=? ORDER BY ts",
                 (0.0, 1.0, "n"))
    assert "ix_incidents_node_ts" in text, text


def test_latest_heartbeat_per_node_seeks(hub_conn):
    """fleet() asks for each node's newest heartbeat on every poll -- the hottest read on the
    hub. It must jump to the tail of the (node, ts) index rather than sort."""
    text = _plan(hub_conn,
                 "SELECT ts FROM heartbeats WHERE node=? ORDER BY ts DESC LIMIT 1", ("n",))
    assert "ix_heartbeats_node_ts" in text and "TEMP B-TREE" not in text, text


def test_incident_samples_by_uid_seeks_the_uid_ts_index(hub_conn):
    """incident_detail() -- the evidence view, hit every time an operator opens an incident --
    loads samples by uid alone, with no node predicate (samples can arrive before their parent
    incident row, so the loader must not join). The (node, uid, ts) index cannot serve that;
    without a uid-leading index this full-scans every sample the hub holds."""
    text = _plan(hub_conn,
                 "SELECT ts, phase, signal, entity, value FROM incident_samples "
                 "WHERE uid=? ORDER BY ts", ("u1",))
    assert "ix_incident_samples_uid_ts" in text, text
    assert "SCAN" not in text, text


def test_fleet_stays_bounded_over_a_large_incident_history(hub_conn):
    """A hub holding a lot of closed history must still answer the live grid. The guard is that
    fleet() is driven by open incidents and the newest heartbeat, not by the size of the log."""
    now = 1_000_000.0
    schema.insert(hub_conn, "heartbeats",
                  [{"ts": now - 10, "interval_s": 300.0}], node="n1")
    schema.insert(hub_conn, "incidents", [
        row for i in range(2000)
        for row in ({"ts": now - 50_000 + i, "uid": f"u{i}", "transition": "open",
                     "signal": "ping.loss", "entity": "1.1.1.1", "severity": "warn",
                     "opened_ts": now - 50_000 + i},
                    {"ts": now - 50_000 + i + 1, "uid": f"u{i}", "transition": "close",
                     "signal": "ping.loss", "entity": "1.1.1.1", "severity": "info",
                     "opened_ts": now - 50_000 + i, "duration_s": 1.0})
    ], node="n1")
    hub_conn.commit()

    r = hubapi.fleet(hub_conn, now)[0]
    assert r["state"] == "ok" and r["open_incidents"] == 0
