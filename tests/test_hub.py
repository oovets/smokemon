"""Hub ingest: first POST inserts, identical replay inserts zero (idempotent via
UNIQUE(node, src_id)), partial overlap inserts only the new rows."""

import gzip
import json
import threading
import time
import urllib.request

import pytest

from smokemon import config, core, hub, schema, ship


@pytest.fixture
def hub_ready(hub_db, monkeypatch):
    """Initialise a hub DB and wire it into the hub module's globals."""
    conn = core.connect(str(hub_db), check_same_thread=False)
    schema.init_hub(conn)
    monkeypatch.setattr(hub, "_conn", conn)
    hub._hub_cols.clear()
    hub._hub_cols.update({
        t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        for t in schema.STD_TABLES
    })
    yield conn
    conn.close()


def _payload(ts0):
    return {
        "node": "testnode",
        "tables": {
            "ping_runs": {
                "columns": ["id", "ts", "target", "sent", "recv", "loss_pct",
                            "rtt_min", "rtt_p25", "rtt_median", "rtt_p75",
                            "rtt_avg", "rtt_max", "rtt_stddev", "node"],
                "rows": [
                    [1, ts0, "1.1.1.1", 20, 20, 0.0, 5, 6, 7, 8, 7, 12, 1.5, "testnode"],
                    [2, ts0 + 10, "1.1.1.1", 20, 20, 0.0, 5, 6, 7, 8, 7, 12, 1.5, "testnode"],
                ],
            },
            "ping_rtts": {
                "columns": ["run_id", "rtt_ms"],
                "rows": [[1, 7.0], [1, 8.0], [2, 7.5]],
            },
            "net_samples": {
                "columns": ["id", "ts", "iface", "ibytes", "obytes", "ipkts", "opkts", "node"],
                "rows": [
                    [10, ts0, "eth0", 1000, 500, 0, 0, "testnode"],
                    [11, ts0 + 10, "eth0", 2000, 1500, 0, 0, "testnode"],
                ],
            },
            "host_samples": {
                "columns": ["id", "ts", "cpu_pct", "load1", "load5", "load15",
                            "mem_used_pct", "mem_total_mb", "temp_c",
                            "disk_read_mbps", "disk_write_mbps",
                            "swap_used_pct", "cache_mb", "oom_kill_count",
                            "psi_cpu", "psi_mem", "psi_io",
                            "cpu_freq_mhz", "cpu_throttle_count", "pi_throttle_bits", "node"],
                "rows": [[20, ts0, 5.0, 0.5, 0.4, 0.3, 30.0, 8192.0, 50.0,
                          1.0, 0.5, 0, 1000, 0, 0.1, 0.2, 0.3, 1500, 0, 0, "testnode"]],
            },
        },
    }


def test_first_ingest(hub_ready):
    ts0 = time.time()
    counts = hub.ingest(_payload(ts0))
    assert counts["ping_runs"] == 2
    assert counts["ping_rtts"] == 3
    assert counts["net_samples"] == 2
    assert counts["host_samples"] == 1


def test_idempotent_replay(hub_ready):
    ts0 = time.time()
    payload = _payload(ts0)
    hub.ingest(payload)
    counts2 = hub.ingest(payload)
    assert all(v == 0 for v in counts2.values()), counts2


def test_partial_overlap_inserts_only_new(hub_ready):
    ts0 = time.time()
    payload = _payload(ts0)
    hub.ingest(payload)
    payload["tables"]["ping_runs"]["rows"].append(
        [3, ts0 + 20, "1.1.1.1", 20, 19, 5.0, 5, 6, 7, 8, 7, 12, 1.5, "testnode"]
    )
    payload["tables"]["ping_rtts"]["rows"] = [[3, 7.2], [3, 7.8]]
    counts = hub.ingest(payload)
    assert counts["ping_runs"] == 1
    assert counts["ping_rtts"] == 2
    assert counts["net_samples"] == 0
    assert counts["host_samples"] == 0


def test_run_map_links_rtts_to_new_run_ids(hub_ready):
    ts0 = time.time()
    hub.ingest(_payload(ts0))
    # All 3 rtts should reference the two ping_run hub ids (not the src ids 1 and 2)
    hub_run_ids = {r[0] for r in hub_ready.execute("SELECT id FROM ping_runs").fetchall()}
    rtt_refs = {r[0] for r in hub_ready.execute("SELECT run_id FROM ping_rtts").fetchall()}
    assert rtt_refs.issubset(hub_run_ids), (rtt_refs, hub_run_ids)


# --- ship-side: raw rtts stay node-local by default, gzipped wire format ---

@pytest.fixture
def node_conn(tmp_db):
    """A node-side DB with a ping_run + its raw rtts, plus the shipper cursor table."""
    conn = core.connect(str(tmp_db))
    schema.init_node(conn)
    ship.init_state(conn)
    rid = schema.insert_one(conn, "ping_runs", {"ts": time.time(), "target": "1.1.1.1",
                                                "sent": 3, "recv": 3, "loss_pct": 0.0})
    conn.executemany("INSERT INTO ping_rtts (run_id, rtt_ms) VALUES (?,?)",
                     [(rid, 1.0), (rid, 2.0), (rid, 3.0)])
    conn.commit()
    yield conn, rid
    conn.close()


def test_rtts_not_shipped_by_default(node_conn, monkeypatch):
    """Default: raw ping_rtts stay node-local; the aggregated ping_run still ships."""
    monkeypatch.setattr(config, "SHIP_RTTS", False)
    conn, _ = node_conn
    payload, maxids = ship.gather(conn)
    assert "ping_runs" in payload
    assert "ping_rtts" not in payload
    assert "ping_rtts" not in maxids


def test_rtts_shipped_when_opted_in(node_conn, monkeypatch):
    """SHIP_RTTS=1: raw rtts ship, capped to already-gathered ping_runs."""
    monkeypatch.setattr(config, "SHIP_RTTS", True)
    conn, rid = node_conn
    payload, maxids = ship.gather(conn)
    assert payload["ping_rtts"]["rows"] == [[rid, 1.0], [rid, 2.0], [rid, 3.0]]
    assert maxids["ping_rtts"] == rid


def test_gzip_ingest_roundtrip(hub_ready, monkeypatch):
    """A gzipped /ingest POST (what ship._post sends) is decompressed and ingested;
    a plain-JSON body still works (back-compat)."""
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    srv = core_http_server()
    try:
        port = srv.server_address[1]
        ts0 = time.time()
        body = gzip.compress(json.dumps(_payload(ts0)).encode())
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/ingest", data=body, method="POST",
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip",
                     "X-Smokemon-Key": "s3cret"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert hub_ready.execute("SELECT COUNT(*) FROM ping_runs").fetchone()[0] == 2


def core_http_server():
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
