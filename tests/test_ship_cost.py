"""Per-node ingest cost: measured wire_bytes x a configurable $/GB rate (hub-side only)."""

from smokemon import config, core, hubapi, schema


def _hub(tmp_path, rows):
    conn = core.connect(str(tmp_path / "hub.db"), check_same_thread=False)
    schema.init_hub(conn)
    conn.executemany("INSERT INTO ingest_log (ts, node, wire_bytes, raw_bytes, rows) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_cost_is_bytes_times_rate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AWS_GB_COST", 0.09)
    now = 1_000_000.0
    # one node, 2 GB shipped over a ~1h window (long enough for the per-day projection)
    conn = _hub(tmp_path, [(now - 3600, "n1", 1_000_000_000, 5_000_000_000, 1000),
                           (now - 60, "n1", 1_000_000_000, 5_000_000_000, 1000)])
    d = hubapi.ship_volume(conn, hours=24.0, now=now)
    conn.close()
    n1 = next(n for n in d["nodes"] if n["node"] == "n1")
    assert n1["cost_window"] == round(2.0 * 0.09, 4)          # 2 GB x $0.09
    assert n1["cost_per_day"] is not None and n1["cost_per_day"] > n1["cost_window"]
    assert d["gb_rate"] == 0.09
    assert d["cost_window_total"] == round(2.0 * 0.09, 2)


def test_rate_zero_yields_zero_cost(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AWS_GB_COST", 0.0)  # AWS data-in is free
    conn = _hub(tmp_path, [(999_999.0, "n1", 5_000_000_000, 9_000_000_000, 1)])
    d = hubapi.ship_volume(conn, hours=24.0, now=1_000_000.0)
    conn.close()
    assert d["cost_per_day_total"] == 0.0
    assert all(n["cost_window"] == 0.0 for n in d["nodes"])
