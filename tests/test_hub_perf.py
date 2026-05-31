"""Hub query performance guards: a plain (ts) index on hub tables so cross-node `WHERE ts >= ?`
windows seek instead of full-scanning, and a bounded latest_metrics. These are the fixes for a
large hub DB making dashboard GETs time out (NetworkError)."""

from smokemon import config, core, hubapi, schema


def _hub(tmp_path):
    conn = core.connect(str(tmp_path / "hub.db"), check_same_thread=False)
    schema.init_hub(conn)
    return conn


def test_hub_tables_have_perf_indexes(tmp_path):
    conn = _hub(tmp_path)
    try:
        for t in ("host_samples", "ping_runs", "net_samples", "port_samples", "device_facts"):
            idx = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (t,))}
            assert f"ix_{t}_ts" in idx, f"{t} is missing the plain (ts) index on the hub"
        # per-entity latest-value index (node, <entity>, ts) for the _IX tables
        pr = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ping_runs'")}
        assert "ix_ping_runs_node_target_ts" in pr
    finally:
        conn.close()


def test_ts_only_window_seeks_index(tmp_path):
    """A cross-node ts-only window must SEARCH the (ts) index, not SCAN the table."""
    conn = _hub(tmp_path)
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM host_samples WHERE ts >= ?", (0.0,)).fetchall()
        text = " ".join(str(r[-1]) for r in plan)
        assert "ix_host_samples_ts" in text, f"expected the (ts) index to be used, got: {text}"
    finally:
        conn.close()


def test_latest_per_target_uses_loose_index_scan(tmp_path):
    """latest_metrics' ping_runs query must jump to each (node,target) max-ts via the
    (node,target,ts) index, not scan the whole (node,ts) index with a temp b-tree."""
    conn = _hub(tmp_path)
    try:
        now = 1_000_000.0
        for tg in ("1.1.1.1", "8.8.8.8"):
            schema.insert(conn, "ping_runs", [{"ts": now - i * 10, "target": tg, "rtt_median": 5.0,
                                               "loss_pct": 0.0} for i in range(500)], node="n")
        conn.commit()
        conn.execute("PRAGMA analysis_limit=400")
        conn.execute("PRAGMA optimize")
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT node, target, rtt_median, loss_pct, MAX(ts) "
            "FROM ping_runs WHERE ts >= ? GROUP BY node, target", (now - 5000,)).fetchall()
        text = " ".join(str(r[-1]) for r in plan)
        assert "ix_ping_runs_node_target_ts" in text and "TEMP B-TREE" not in text, text
    finally:
        conn.close()


def test_latest_metrics_window_excludes_long_silent_nodes(tmp_path, monkeypatch):
    conn = _hub(tmp_path)
    try:
        now = 1_000_000.0
        schema.insert(conn, "host_samples", [{"ts": now - 100, "cpu_pct": 10.0}], node="fresh")
        schema.insert(conn, "host_samples", [{"ts": now - 60 * 86400, "cpu_pct": 20.0}], node="stale")
        conn.commit()

        monkeypatch.setattr(config, "HUB_LATEST_WINDOW_S", 30 * 86400)
        latest = hubapi.latest_metrics(conn, now=now)
        assert "fresh" in latest and "stale" not in latest  # silent >30d drops out of "latest"

        monkeypatch.setattr(config, "HUB_LATEST_WINDOW_S", 0)  # unbounded = legacy behaviour
        both = hubapi.latest_metrics(conn, now=now)
        assert "fresh" in both and "stale" in both
    finally:
        conn.close()
