"""Query loaders: empty DB returns empty, populated DB returns expected shapes,
counter-deltas (rates) skip the first sample and handle resets / None correctly."""

from smokemon import core, query, schema


def _seed_minimal(conn, ts0):
    """Insert enough varied data so every loader returns at least one point."""
    for i in range(5):
        ts = ts0 + i * 30
        schema.insert(conn, "ping_runs", [{
            "ts": ts, "target": "1.1.1.1", "sent": 20, "recv": 20, "loss_pct": 0.0,
            "rtt_min": 5.0, "rtt_p25": 6.0, "rtt_median": 7.0, "rtt_p75": 8.0,
            "rtt_avg": 7.0, "rtt_max": 12.0, "rtt_stddev": 1.5,
        }])
        schema.insert(conn, "host_samples", [{
            "ts": ts, "cpu_pct": 30 + i, "load1": 1.2, "load5": 1.0, "load15": 0.9,
            "mem_used_pct": 40, "mem_total_mb": 8000, "temp_c": 55.0,
            "disk_read_mbps": 1.0, "disk_write_mbps": 0.5,
            "swap_used_pct": 0, "cache_mb": 1500.0, "oom_kill_count": 0,
            "psi_cpu": 0.5, "psi_mem": 0.1, "psi_io": 0.2,
            "cpu_freq_mhz": 1500.0 - i * 10, "cpu_throttle_count": i,
            "pi_throttle_bits": 0,
        }])
        schema.insert(conn, "wifi_samples", [{
            "ts": ts, "ssid": "Net", "channel": "5180", "phy_mode": "ac",
            "rssi_dbm": -50 - i, "noise_dbm": -90, "tx_rate_mbps": 866.0,
            "bssid": "aa:bb:cc:dd:ee:01" if i < 3 else "aa:bb:cc:dd:ee:02",
            "retry_count": i * 5, "discard_count": i, "beacon_loss": 0,
        }])
        schema.insert(conn, "tcp_samples", [{
            "ts": ts, "retrans_segs": i * 3, "out_rsts": i, "estab_resets": 0,
            "udp_in_errors": 0, "udp_no_ports": 0,
            "conntrack_used": 1000 + i * 10, "conntrack_max": 65536,
        }])
        schema.insert(conn, "thermal_zones", [
            {"ts": ts, "zone": "cpu", "temp_c": 55.0 + i * 0.5},
            {"ts": ts, "zone": "gpu", "temp_c": 48.0},
        ])
        schema.insert(conn, "power_samples", [{
            "ts": ts, "rail": "VDD_CPU", "watts": 1.2, "volts": 0.85, "amps": 1.4,
        }])
        schema.insert(conn, "net_samples", [{
            "ts": ts, "iface": "eth0",
            "ibytes": i * 10_000_000, "obytes": i * 5_000_000,
            "ipkts": 0, "opkts": 0,
        }])
        schema.insert(conn, "disk_samples", [{
            "ts": ts, "mount": "/", "used_pct": 60.0, "free_gb": 100.0, "inode_used_pct": 25.0,
        }])
    conn.commit()


def test_empty_db_returns_empty(tmp_db):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    now = 1_000_000.0
    assert query.load_ping_smoke(conn, now - 60, now, None) == {}
    assert query.load_host(conn, now - 60, now) == {}
    assert query.load_wifi(conn, now - 60, now) == {}
    assert query.load_tcp(conn, now - 60, now) == {}
    assert query.load_thermal(conn, now - 60, now) == {}
    assert query.load_power(conn, now - 60, now) == {}
    assert query.load_psi(conn, now - 60, now) == {}
    assert query.load_freq(conn, now - 60, now) == {}
    conn.close()


def test_loaders_return_data(tmp_db, ts0):
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    _seed_minimal(conn, ts0)

    since, until = ts0 - 60, ts0 + 600

    ping = query.load_ping_smoke(conn, since, until, None)
    assert "1.1.1.1" in ping
    assert len(ping["1.1.1.1"]["p50"]) == 5

    host = query.load_host(conn, since, until)
    assert len(host["t"]) == 5
    assert host["cpu"][0] == 30

    wifi = query.load_wifi(conn, since, until)
    assert wifi["roams"] == 1
    assert wifi["bssids_seen"] == 2

    tcp = query.load_tcp(conn, since, until)
    nonzero = [v for v in tcp["retrans"] if v and v > 0]
    assert nonzero, "retrans rate should compute after the first sample"

    thermal = query.load_thermal(conn, since, until)
    assert set(thermal) == {"cpu", "gpu"}

    power = query.load_power(conn, since, until)
    assert "VDD_CPU" in power

    psi = query.load_psi(conn, since, until)
    assert len(psi["t"]) == 5

    freq = query.load_freq(conn, since, until)
    assert len(freq["t"]) == 5
    assert freq["mhz"][0] == 1500.0

    net = query.load_net(conn, since, until)
    assert "eth0" in net

    conn.close()


def test_rate_handles_none_and_resets():
    """_rate must return None for missing samples, counter resets (negative diff),
    and zero/negative dt. Critical so renderers never see bogus spikes."""
    from smokemon.query import _rate
    assert _rate(None, 10, 100.0, 90.0) is None
    assert _rate(10, None, 100.0, 90.0) is None
    assert _rate(10, 20, 100.0, 90.0) is None  # negative diff (counter reset)
    assert _rate(20, 10, 100.0, 100.0) is None  # dt == 0
    assert _rate(20, 10, 100.0, 90.0) == 1.0  # 10 events / 10 s


def test_load_ping_smoke_legacy_fallback(tmp_db, ts0):
    """Rows written before the rtt_p25/p75 migration have those columns NULL. The PNG
    loader must rebuild p25/p75 from ping_rtts via _percentiles_for instead of plotting
    NaN. This path is otherwise never exercised (the normal seed pre-populates p25/p75)."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    # Legacy row: median set, but p25/p75 left NULL (omitted from the insert dict).
    rid = schema.insert_one(conn, "ping_runs", {
        "ts": ts0, "target": "1.1.1.1", "sent": 10, "recv": 10, "loss_pct": 0.0,
        "rtt_min": 5.0, "rtt_median": 7.0, "rtt_max": 12.0,
    })
    conn.executemany("INSERT INTO ping_rtts (run_id, rtt_ms) VALUES (?,?)",
                     [(rid, v) for v in (5.0, 6.0, 7.0, 8.0, 10.0, 12.0)])
    conn.commit()

    data = query.load_ping_smoke(conn, ts0 - 60, ts0 + 60, ["1.1.1.1"])
    d = data["1.1.1.1"]
    import statistics
    exp_p25, _exp_p50, exp_p75 = statistics.quantiles([5.0, 6.0, 7.0, 8.0, 10.0, 12.0], n=4)
    assert d["p25"][0] == exp_p25
    assert d["p75"][0] == exp_p75
    # p0/p50/p100 still come straight from the row's min/median/max columns.
    assert d["p0"][0] == 5.0 and d["p50"][0] == 7.0 and d["p100"][0] == 12.0
    conn.close()


def test_load_ping_smoke_legacy_single_rtt(tmp_db, ts0):
    """Legacy row with a single rtt: quantiles can't run (<2), so p25/p75 fall back to
    the median rather than raising."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    rid = schema.insert_one(conn, "ping_runs", {
        "ts": ts0, "target": "1.1.1.1", "sent": 1, "recv": 1, "loss_pct": 0.0,
        "rtt_min": 9.0, "rtt_median": 9.0, "rtt_max": 9.0,
    })
    conn.execute("INSERT INTO ping_rtts (run_id, rtt_ms) VALUES (?,?)", (rid, 9.0))
    conn.commit()
    d = query.load_ping_smoke(conn, ts0 - 60, ts0 + 60, ["1.1.1.1"])["1.1.1.1"]
    assert d["p25"][0] == 9.0 and d["p75"][0] == 9.0
    conn.close()


def test_load_net_lag_python_parity(tmp_db, ts0, monkeypatch):
    """The Python fallback (SQLite < 3.25) must yield the same per-interface series as
    the SQL LAG() path. CI normally only hits LAG; force the fallback and compare."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    for i in range(5):
        schema.insert(conn, "net_samples", [{
            "ts": ts0 + i * 10, "iface": "eth0",
            "ibytes": i * 10_000_000, "obytes": i * 5_000_000, "ipkts": 0, "opkts": 0,
        }])
    conn.commit()
    since, until = ts0 - 60, ts0 + 600

    monkeypatch.setattr(query, "_HAS_LAG", True)
    lag = query.load_net(conn, since, until)
    monkeypatch.setattr(query, "_HAS_LAG", False)
    py = query.load_net(conn, since, until)
    assert lag["eth0"]["in"] == py["eth0"]["in"]
    assert lag["eth0"]["out"] == py["eth0"]["out"]
    assert lag["eth0"]["t"] == py["eth0"]["t"]
    conn.close()


def test_node_filter(tmp_db, ts0):
    """Inserting under different node names and then filtering by --node must isolate
    each node's rows. Used on hub DBs that hold many nodes."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    schema.insert(conn, "host_samples",
                  [{"ts": ts0, "cpu_pct": 10}], node="node-a")
    schema.insert(conn, "host_samples",
                  [{"ts": ts0, "cpu_pct": 99}], node="node-b")
    conn.commit()
    a = query.load_host(conn, ts0 - 60, ts0 + 60, node="node-a")
    b = query.load_host(conn, ts0 - 60, ts0 + 60, node="node-b")
    assert a["cpu"] == [10]
    assert b["cpu"] == [99]
    conn.close()
