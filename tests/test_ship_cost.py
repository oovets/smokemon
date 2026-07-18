"""Per-node ship volume, from the hub's own per-POST accounting.

wire_bytes is measured at ingest (the compressed body that actually crossed the network), never
estimated from row counts -- the whole point of the incident pivot is a shrinking write budget,
and a self-reported estimate would be the one number guaranteed to flatter it.
"""

from smokemon import hubapi

NOW = 1_000_000.0


def _log(conn, rows):
    conn.executemany(
        "INSERT INTO ingest_log (ts, node, wire_bytes, raw_bytes, rows) VALUES (?,?,?,?,?)", rows)
    conn.commit()


# ---------- ship_volume ----------

def test_volume_sums_per_node_and_projects_a_daily_rate(hub_conn):
    _log(hub_conn, [(NOW - 3600, "n1", 1000, 5000, 10),
                    (NOW - 60, "n1", 1000, 5000, 10),
                    (NOW - 60, "n2", 400, 900, 4)])
    d = hubapi.ship_volume(hub_conn, hours=24.0, now=NOW)

    n1 = next(n for n in d["nodes"] if n["node"] == "n1")
    assert n1["posts"] == 2 and n1["wire_bytes"] == 2000
    assert n1["raw_bytes"] == 10000 and n1["rows"] == 20
    assert d["wire_bytes"] == 2400
    assert d["per_day_bytes"] == 2400            # a 24h window projects to itself
    assert [n["node"] for n in d["nodes"]] == ["n1", "n2"]   # biggest talker first


def test_volume_projection_scales_a_short_window(hub_conn):
    _log(hub_conn, [(NOW - 60, "n1", 1000, 2000, 5)])
    d = hubapi.ship_volume(hub_conn, hours=6.0, now=NOW)
    assert d["per_day_bytes"] == 4000            # 1000 bytes in 6h -> 4000/day


def test_volume_excludes_posts_outside_the_window(hub_conn):
    _log(hub_conn, [(NOW - 100_000, "old", 9999, 9999, 9),
                    (NOW - 60, "n1", 100, 200, 1)])
    d = hubapi.ship_volume(hub_conn, hours=1.0, now=NOW)
    assert [n["node"] for n in d["nodes"]] == ["n1"] and d["wire_bytes"] == 100


def test_volume_on_a_hub_that_has_received_nothing(hub_conn):
    d = hubapi.ship_volume(hub_conn, hours=24.0, now=NOW)
    assert d["nodes"] == [] and d["wire_bytes"] == 0 and d["per_day_bytes"] == 0


def test_volume_zero_hours_does_not_divide_by_zero(hub_conn):
    _log(hub_conn, [(NOW, "n1", 100, 200, 1)])
    assert hubapi.ship_volume(hub_conn, hours=0.0, now=NOW)["per_day_bytes"] == 0


# ---------- ingest_rate (the in-memory ring, no table involved) ----------

def test_ingest_rate_from_the_live_ring():
    events = [(NOW - 600, 1000, 4000, 10), (NOW - 300, 2000, 8000, 20)]
    r = hubapi.ingest_rate(events, now=NOW, window_s=900.0)
    assert r["posts"] == 2 and r["window_s"] == 900.0
    assert r["wire_bps"] == 5.0                  # 3000 bytes over the 600s span
    assert r["rows_per_s"] == 0.05


def test_ingest_rate_ignores_events_older_than_the_window():
    events = [(NOW - 5000, 9999, 9999, 999), (NOW - 100, 1000, 2000, 10)]
    r = hubapi.ingest_rate(events, now=NOW, window_s=900.0)
    assert r["posts"] == 1


def test_ingest_rate_with_no_traffic_is_zero_not_a_crash():
    r = hubapi.ingest_rate([], now=NOW)
    assert r == {"posts": 0, "wire_bps": 0.0, "rows_per_s": 0.0, "window_s": 900.0}
