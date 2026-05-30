import importlib
import sys
from datetime import datetime

from smokemon import core, footprint, schema


def _sample_db(path, ts0):
    conn = core.connect(str(path))
    schema.init_node(conn)
    r1 = schema.insert_one(conn, "ping_runs", {
        "ts": ts0,
        "target": "1.1.1.1",
        "sent": 2,
        "recv": 2,
        "loss_pct": 0.0,
        "rtt_min": 5.0,
        "rtt_p25": 5.0,
        "rtt_median": 6.0,
        "rtt_p75": 7.0,
        "rtt_avg": 6.0,
        "rtt_max": 7.0,
        "rtt_stddev": 1.0,
    })
    r2 = schema.insert_one(conn, "ping_runs", {
        "ts": ts0 + 10,
        "target": "1.1.1.1",
        "sent": 1,
        "recv": 1,
        "loss_pct": 0.0,
        "rtt_min": 8.0,
        "rtt_p25": 8.0,
        "rtt_median": 8.0,
        "rtt_p75": 8.0,
        "rtt_avg": 8.0,
        "rtt_max": 8.0,
        "rtt_stddev": 0.0,
    })
    conn.executemany("INSERT INTO ping_rtts (run_id, rtt_ms) VALUES (?,?)", [(r1, 5.0), (r1, 7.0), (r2, 8.0)])
    schema.insert(conn, "net_samples", [{
        "ts": ts0 + 10,
        "iface": "eth0",
        "ibytes": 100,
        "obytes": 200,
        "ipkts": 1,
        "opkts": 2,
    }])
    conn.commit()
    return conn


def test_footprint_counts_collector_rows_and_default_ship_excludes_rtts(tmp_db, ts0):
    conn = _sample_db(tmp_db, ts0)
    try:
        fp = footprint.analyze(conn, str(tmp_db), ts0 - 1, ts0 + 11)
    finally:
        conn.close()
    assert fp.collector_rows == 6
    assert fp.raw_rtts.rows == 3
    assert fp.ship_rows == 3
    assert fp.ship_gzip_bytes > 0
    assert fp.collector_rows_per_day == 6 * 86400 / 10
    out = footprint.render(fp)
    assert "raw ping RTTs: 3 rows local-only" in out
    assert "ping_runs" in out and "net_samples" in out


def test_footprint_can_include_raw_rtts_in_ship_estimate(tmp_db, ts0):
    conn = _sample_db(tmp_db, ts0)
    try:
        fp = footprint.analyze(conn, str(tmp_db), ts0 - 1, ts0 + 11, ship_rtts=True)
    finally:
        conn.close()
    assert fp.ship_rows == 6
    assert "included in ship estimate" in footprint.render(fp)


def test_hub_db_ship_payload_uses_src_id_as_node_id(hub_db, ts0):
    conn = core.connect(str(hub_db))
    schema.init_hub(conn)
    conn.execute(
        "INSERT INTO ping_runs (ts,target,sent,recv,loss_pct,rtt_median,node,src_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts0, "1.1.1.1", 1, 1, 0.0, 5.0, "pi01", 42),
    )
    conn.commit()
    try:
        payload, counts = footprint._ship_payload(conn, ts0 - 1, ts0 + 1, "pi01", False)
    finally:
        conn.close()
    assert counts == {"ping_runs": 1}
    assert payload["ping_runs"]["columns"][0] == "id"
    assert "src_id" not in payload["ping_runs"]["columns"]
    assert payload["ping_runs"]["rows"][0][0] == 42


def test_cli_footprint_subcommand(tmp_db, ts0, monkeypatch, capsys):
    conn = _sample_db(tmp_db, ts0)
    conn.close()
    import smokemon.config
    import smokemon.cli

    importlib.reload(smokemon.config)
    importlib.reload(smokemon.cli)
    monkeypatch.setattr(sys, "argv", [
        "smoke",
        "footprint",
        "--db",
        str(tmp_db),
        "--since",
        datetime.fromtimestamp(ts0 - 1).isoformat(),
        "--until",
        datetime.fromtimestamp(ts0 + 11).isoformat(),
    ])
    assert smokemon.cli.main() == 0
    out = capsys.readouterr().out
    assert "footprint:" in out
    assert "collector:" in out
    assert "ship estimate:" in out
