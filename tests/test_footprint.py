import importlib
import sys
from datetime import datetime

from smokemon import core, footprint, schema


def _sample_db(path, ts0):
    conn = core.connect(str(path))
    schema.init_node(conn)
    schema.insert(conn, "incidents", [
        {"ts": ts0, "uid": "u1", "transition": "open", "signal": "rtt", "entity": "1.1.1.1",
         "severity": "warn", "value": 120.0, "opened_ts": ts0},
        {"ts": ts0 + 10, "uid": "u1", "transition": "close", "signal": "rtt", "entity": "1.1.1.1",
         "severity": "info", "opened_ts": ts0, "duration_s": 10.0, "worst_value": 140.0},
    ])
    schema.insert(conn, "incident_samples", [
        {"ts": ts0, "uid": "u1", "phase": "pre", "signal": "rtt", "entity": "1.1.1.1", "value": 118.0},
        {"ts": ts0 + 5, "uid": "u1", "phase": "during", "signal": "rtt", "entity": "1.1.1.1", "value": 140.0},
    ])
    schema.insert(conn, "heartbeats", [{"ts": ts0 + 10, "interval_s": 300.0, "cpu_pct": 3.0}])
    conn.commit()
    return conn


def test_footprint_counts_collector_rows_and_ship_estimate(tmp_db, ts0):
    conn = _sample_db(tmp_db, ts0)
    try:
        fp = footprint.analyze(conn, str(tmp_db), ts0 - 1, ts0 + 11)
    finally:
        conn.close()
    assert fp.collector_rows == 5
    assert fp.ship_rows == 5
    assert fp.ship_gzip_bytes > 0
    assert fp.collector_rows_per_day == 5 * 86400 / 10
    out = footprint.render(fp)
    assert "incidents" in out and "heartbeats" in out


def test_hub_db_ship_payload_uses_src_id_as_node_id(hub_db, ts0):
    conn = core.connect(str(hub_db))
    schema.init_hub(conn)
    conn.execute(
        "INSERT INTO heartbeats (ts,interval_s,cpu_pct,node,src_id) VALUES (?,?,?,?,?)",
        (ts0, 300.0, 3.0, "pi01", 42),
    )
    conn.commit()
    try:
        payload, counts = footprint._ship_payload(conn, ts0 - 1, ts0 + 1, "pi01")
    finally:
        conn.close()
    assert counts == {"heartbeats": 1}
    assert payload["heartbeats"]["columns"][0] == "id"
    assert "src_id" not in payload["heartbeats"]["columns"]
    assert payload["heartbeats"]["rows"][0][0] == 42


def test_cli_footprint_subcommand(tmp_db, ts0, monkeypatch, capsys):
    conn = _sample_db(tmp_db, ts0)
    conn.close()
    import smokemon.cli
    import smokemon.config

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
