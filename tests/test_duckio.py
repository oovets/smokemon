"""Optional DuckDB read acceleration - smokemon.duckio.

The DuckDB path is exercised for real when duckdb is importable (importorskip otherwise); the
graceful-fallback path is tested by forcing _HAS_DUCKDB off. Parity check: DuckDB reading the
hub SQLite file in place must return the same counts as sqlite3."""

import pytest

from smokemon import core, duckio, schema


def _seed_hub(tmp_db):
    conn = core.connect(str(tmp_db))
    schema.init_hub(conn)
    base = 1_000_000.0
    for i in range(5):
        schema.insert(conn, "ping_runs", [{
            "ts": base + i * 10, "target": "1.1.1.1", "sent": 20, "recv": 20, "loss_pct": 0.0,
            "rtt_min": 5.0, "rtt_median": 7.0, "rtt_max": 12.0}], node="pi01")
    conn.commit()
    return conn


def test_available_reflects_import_state():
    # available() must agree with whether duckdb actually imported.
    assert duckio.available() == duckio._HAS_DUCKDB


def test_connect_ro_none_without_duckdb(tmp_db, monkeypatch):
    monkeypatch.setattr(duckio, "_HAS_DUCKDB", False)
    assert duckio.connect_ro(str(tmp_db)) is None


def test_duckdb_reads_same_counts_as_sqlite(tmp_db):
    pytest.importorskip("duckdb")
    assert duckio.available()
    conn = _seed_hub(tmp_db)
    sqlite_n = conn.execute("SELECT COUNT(*) FROM ping_runs").fetchone()[0]
    conn.close()

    con = duckio.connect_ro(str(tmp_db))
    assert con is not None, "duckdb importable -> connect_ro should attach the hub DB"
    duck_rows = duckio.query_rows(con, "SELECT COUNT(*) FROM ping_runs")
    assert duck_rows[0][0] == sqlite_n == 5
    # a GROUP BY aggregate (the kind the heatmap runs) returns the expected single group
    agg = duckio.query_rows(con, "SELECT node, COUNT(*) FROM ping_runs GROUP BY node")
    assert agg == [("pi01", 5)]
    con.close()
