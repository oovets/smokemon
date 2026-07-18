"""Query loaders: the reduction of the append-only incidents log into one dict per incident,
standalone sample reads, heartbeat lookups and the orphan health metric."""

from smokemon import core, query, schema


def _open(ts, uid, **kw):
    row = {"ts": ts, "uid": uid, "transition": "open", "signal": "rtt", "entity": "1.1.1.1",
           "severity": "warn", "value": 120.0, "opened_ts": ts}
    row.update(kw)
    return row


def _close(ts, uid, opened_ts, **kw):
    row = {"ts": ts, "uid": uid, "transition": "close", "signal": "rtt", "entity": "1.1.1.1",
           "severity": "info", "opened_ts": opened_ts, "duration_s": ts - opened_ts,
           "worst_value": 180.0}
    row.update(kw)
    return row


def test_empty_db_returns_empty(node_db):
    assert query.load_incidents(node_db, 0, 1e12) == []
    assert query.load_incident_samples(node_db, "nope") == []
    assert query.load_heartbeats(node_db, 0, 1e12) == []
    assert query.latest_heartbeat(node_db, "pi01") is None
    assert query.orphan_stats(node_db) == (0, 0.0)


def test_load_incidents_reduces_transitions_to_one_row(node_db, ts0):
    schema.insert(node_db, "incidents", [_open(ts0, "u1"), _close(ts0 + 45, "u1", ts0)])
    node_db.commit()
    (inc,) = query.load_incidents(node_db, ts0 - 60, ts0 + 120)
    assert inc["uid"] == "u1" and inc["signal"] == "rtt" and inc["entity"] == "1.1.1.1"
    assert inc["state"] == "closed"
    assert inc["opened_ts"] == ts0 and inc["ended_ts"] == ts0 + 45
    assert inc["duration_s"] == 45.0
    assert inc["worst_value"] == 180.0
    # The close row carries severity 'info'; the incident must keep what the open evaluated,
    # otherwise every resolved incident reads as harmless in hindsight.
    assert inc["severity"] == "warn"
    assert [t["transition"] for t in inc["transitions"]] == ["open", "close"]


def test_load_incidents_open_with_no_terminal_is_ongoing(node_db, ts0):
    schema.insert(node_db, "incidents", [_open(ts0, "u1", severity="crit")])
    node_db.commit()
    (inc,) = query.load_incidents(node_db, ts0 - 60, ts0 + 120)
    assert inc["state"] == "ongoing"
    assert inc["ended_ts"] is None and inc["duration_s"] is None
    assert inc["severity"] == "crit"


def test_reopen_after_close_is_ongoing_again(node_db, ts0):
    """A reopen legitimately follows a close inside one uid, so state is decided by which
    transition came LAST - not by whether a close exists at all."""
    schema.insert(node_db, "incidents", [
        _open(ts0, "u1"),
        _close(ts0 + 30, "u1", ts0),
        {"ts": ts0 + 60, "uid": "u1", "transition": "reopen", "signal": "rtt",
         "entity": "1.1.1.1", "severity": "error", "opened_ts": ts0},
    ])
    node_db.commit()
    (inc,) = query.load_incidents(node_db, ts0 - 60, ts0 + 120)
    assert inc["state"] == "ongoing" and inc["ended_ts"] is None
    assert inc["severity"] == "error"


def test_stale_and_expired_also_end_an_incident(node_db, ts0):
    for i, terminal in enumerate(("stale", "expired")):
        uid = f"u{i}"
        schema.insert(node_db, "incidents", [
            _open(ts0, uid),
            {"ts": ts0 + 10, "uid": uid, "transition": terminal, "signal": "rtt",
             "entity": "1.1.1.1", "severity": "info", "opened_ts": ts0, "duration_s": 10.0},
        ])
    node_db.commit()
    states = {i["uid"]: i["state"] for i in query.load_incidents(node_db, ts0 - 60, ts0 + 120)}
    assert states == {"u0": "closed", "u1": "closed"}


def test_load_incidents_newest_first_and_node_filtered(hub_db, ts0):
    conn = core.connect(str(hub_db))
    schema.init_hub(conn)
    for i, (node, uid, ts) in enumerate((("pi01", "a", ts0), ("app01", "b", ts0 + 100),
                                         ("pi01", "c", ts0 + 200))):
        conn.execute("INSERT INTO incidents (ts,uid,transition,signal,severity,opened_ts,node,src_id) "
                     "VALUES (?,?,'open','rtt','warn',?,?,?)", (ts, uid, ts, node, i))
    conn.commit()
    assert [i["uid"] for i in query.load_incidents(conn, ts0 - 60, ts0 + 300)] == ["c", "b", "a"]
    assert [i["uid"] for i in query.load_incidents(conn, ts0 - 60, ts0 + 300, "pi01")] == ["c", "a"]
    conn.close()


def test_load_incidents_recovers_opened_ts_from_a_late_window(node_db, ts0):
    """A window that catches only the close still reports the true start, because the node
    stamps opened_ts on every transition row."""
    schema.insert(node_db, "incidents", [_open(ts0, "u1"), _close(ts0 + 600, "u1", ts0)])
    node_db.commit()
    (inc,) = query.load_incidents(node_db, ts0 + 300, ts0 + 900)
    assert inc["opened_ts"] == ts0 and inc["duration_s"] == 600.0


def test_load_incident_samples_returns_orphans(node_db, ts0):
    """Samples ship ahead of their parent transition, so a sample whose incident row has not
    arrived yet must still be readable - it is the evidence of what happened."""
    schema.insert(node_db, "incident_samples", [
        {"ts": ts0 + 5, "uid": "orphan", "phase": "during", "signal": "rtt",
         "entity": "1.1.1.1", "value": 150.0},
        {"ts": ts0, "uid": "orphan", "phase": "pre", "signal": "rtt",
         "entity": "1.1.1.1", "value": 12.0},
    ])
    node_db.commit()
    got = query.load_incident_samples(node_db, "orphan")
    assert [s["phase"] for s in got] == ["pre", "during"]     # ordered by ts, not insert order
    assert got[1]["value"] == 150.0


def test_orphan_stats_counts_and_ages_unjoined_samples(node_db, ts0):
    schema.insert(node_db, "incidents", [_open(ts0, "u1")])
    schema.insert(node_db, "incident_samples", [
        {"ts": ts0, "uid": "u1", "phase": "pre", "signal": "rtt", "value": 10.0},
        {"ts": ts0 + 1, "uid": "ghost", "phase": "during", "signal": "rtt", "value": 99.0},
        {"ts": ts0 + 2, "uid": "ghost", "phase": "post", "signal": "rtt", "value": 11.0},
    ])
    node_db.commit()
    count, age = query.orphan_stats(node_db, now=ts0 + 61)
    assert count == 2
    assert age == 60.0


def test_heartbeat_loaders(node_db, ts0):
    schema.insert(node_db, "heartbeats", [
        {"ts": ts0, "interval_s": 300.0, "cpu_pct": 3.0, "db_mb": 1.5},
        {"ts": ts0 + 300, "interval_s": 300.0, "cpu_pct": 4.0, "db_mb": 1.6},
    ], node="pi01")
    node_db.commit()
    rows = query.load_heartbeats(node_db, ts0 - 60, ts0 + 600)
    assert [r["cpu_pct"] for r in rows] == [3.0, 4.0]
    assert rows[0]["node"] == "pi01" and rows[0]["interval_s"] == 300.0
    latest = query.latest_heartbeat(node_db, "pi01")
    assert latest["ts"] == ts0 + 300 and latest["db_mb"] == 1.6
    assert query.latest_heartbeat(node_db, "other") is None


def test_load_ext_events_newest_last_and_capped(node_db, ts0):
    schema.insert(node_db, "ext_events", [
        {"ts": ts0 + i, "source": "app", "severity": "warn", "event": f"e{i}", "detail": ""}
        for i in range(5)])
    node_db.commit()
    got = query.load_ext_events(node_db, ts0 - 10, ts0 + 10, limit=3)
    assert [e["event"] for e in got] == ["e2", "e3", "e4"]


def test_window_and_helpers():
    since, until = query.window(2.0, None, None, None)
    assert 7195 < until - since < 7205
    assert query.host_label("https://www.example.com/x") == "example"
    assert query.last_value([1.0, None, 3.0, None]) == 3.0
    assert query.last_value([None]) is None
