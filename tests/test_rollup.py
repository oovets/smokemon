"""Hub-side rollups (downsampling) - smokemon.rollup + the resolution-aware query loaders.

Seeds a hub-schema DB, runs rollup(), and checks per-column aggregation, incrementality, and
that the open (in-progress) bucket is left untouched. All stdlib, against a temp DB."""

from smokemon import core, query, rollup, schema


def _hub(tmp_db):
    conn = core.connect(str(tmp_db))
    schema.init_hub(conn)
    return conn


def test_rollup_tables_exist_after_init_hub(tmp_db):
    conn = _hub(tmp_db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "host_samples_1m" in names and "host_samples_1h" in names
    assert "ping_runs_1m" in names and "rollup_state" in names
    conn.close()


def test_rollup_aggregates_minute_buckets(tmp_db):
    conn = _hub(tmp_db)
    # Two full minutes of host samples (cpu 10/20/30 then 40/50/60), all well in the past so both
    # minute buckets are closed. mean per minute = 20 and 50.
    base = 1_000_000.0 - 100_000.0
    base = base - (base % 60)  # align to a minute boundary so the buckets are clean
    rows = []
    for i, cpu in enumerate([10, 20, 30]):
        rows.append({"ts": base + i * 20, "cpu_pct": cpu, "mem_used_pct": 40.0, "temp_c": 50.0})
    for i, cpu in enumerate([40, 50, 60]):
        rows.append({"ts": base + 60 + i * 20, "cpu_pct": cpu, "mem_used_pct": 40.0, "temp_c": 50.0})
    schema.insert(conn, "host_samples", rows, node="pi01")
    conn.commit()

    written = rollup.rollup(conn, now=base + 1_000)  # both minutes long since closed
    assert written.get(("host_samples", "_1m"), 0) == 2

    got = conn.execute("SELECT bucket_ts, cpu_pct FROM host_samples_1m WHERE node='pi01' "
                       "ORDER BY bucket_ts").fetchall()
    assert [r[0] for r in got] == [base, base + 60]
    assert abs(got[0][1] - 20.0) < 1e-9 and abs(got[1][1] - 50.0) < 1e-9
    conn.close()


def test_rollup_is_incremental_and_skips_open_bucket(tmp_db):
    conn = _hub(tmp_db)
    base = 1_000_000.0
    base = base - (base % 60)
    # one closed minute + one sample in the currently-open minute (relative to `now`).
    schema.insert(conn, "host_samples", [
        {"ts": base + 10, "cpu_pct": 10.0}, {"ts": base + 30, "cpu_pct": 30.0},
        {"ts": base + 65, "cpu_pct": 99.0},  # lands in the bucket that is still open at `now`
    ], node="pi01")
    conn.commit()
    now = base + 80  # open bucket starts at base+60, so the base+65 sample is not yet closed
    w1 = rollup.rollup(conn, now=now)
    assert w1.get(("host_samples", "_1m"), 0) == 1  # only the first, closed minute
    rows = conn.execute("SELECT bucket_ts FROM host_samples_1m WHERE node='pi01'").fetchall()
    assert [r[0] for r in rows] == [base]

    # second pass with no new closed bucket writes nothing
    w2 = rollup.rollup(conn, now=now + 5)
    assert w2.get(("host_samples", "_1m"), 0) == 0

    # once `now` advances past the second minute, that bucket closes and is rolled up
    w3 = rollup.rollup(conn, now=base + 130)
    assert w3.get(("host_samples", "_1m"), 0) == 1
    rows = conn.execute("SELECT bucket_ts, cpu_pct FROM host_samples_1m WHERE node='pi01' "
                        "ORDER BY bucket_ts").fetchall()
    assert [r[0] for r in rows] == [base, base + 60]
    assert abs(rows[1][1] - 99.0) < 1e-9
    conn.close()


def test_rollup_ping_groups_by_target(tmp_db):
    """ping_runs rolls up per (node, target) bucket, and loss_pct takes MAX (a spike survives)."""
    conn = _hub(tmp_db)
    base = 990_000.0
    base = base - (base % 60)
    rows = []
    for i, (loss, med) in enumerate([(0.0, 8.0), (50.0, 9.0), (0.0, 10.0)]):
        rows.append({"ts": base + i * 20, "target": "1.1.1.1", "sent": 20, "recv": 18,
                     "loss_pct": loss, "rtt_min": 5.0, "rtt_median": med, "rtt_max": med + 2})
    schema.insert(conn, "ping_runs", rows, node="pi01")
    conn.commit()
    rollup.rollup(conn, now=base + 1_000)
    got = conn.execute("SELECT target, loss_pct, rtt_median FROM ping_runs_1m "
                       "WHERE node='pi01'").fetchall()
    assert len(got) == 1 and got[0][0] == "1.1.1.1"
    assert got[0][1] == 50.0  # MAX loss kept the spike
    assert abs(got[0][2] - 9.0) < 1e-9  # AVG of 8/9/10
    conn.close()


# ---------- resolution selection ----------

def test_resolution_thresholds():
    assert query._resolution(0.0, 3 * 3600) == ""        # 3h -> raw
    assert query._resolution(0.0, 24 * 3600) == "_1m"     # 1d -> 1m
    assert query._resolution(0.0, 30 * 86400) == "_1h"    # 30d -> 1h


def test_load_host_reads_rollup_and_falls_back(tmp_db):
    conn = _hub(tmp_db)
    base = 980_000.0
    base = base - (base % 60)
    schema.insert(conn, "host_samples", [
        {"ts": base + i * 20, "cpu_pct": 20.0 + i, "mem_used_pct": 40.0} for i in range(6)
    ], node="pi01")
    conn.commit()
    rollup.rollup(conn, now=base + 1_000)
    # explicit _1m resolution reads the rollup table and keeps the same dict shape
    d = query.load_host(conn, base - 60, base + 200, node="pi01", res="_1m")
    assert {"t", "cpu", "mem"} <= set(d) and len(d["t"]) >= 1
    # an empty _1h table -> fall back to raw rather than returning nothing
    d2 = query.load_host(conn, base - 60, base + 200, node="pi01", res="_1h")
    assert d2["t"], "must fall back to raw when the rollup table has no rows in range"
    conn.close()
