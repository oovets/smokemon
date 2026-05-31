"""Hub ingest: first POST inserts, identical replay inserts zero (idempotent via
UNIQUE(node, src_id)), partial overlap inserts only the new rows."""

import gzip
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from smokemon import config, core, hub, hubapi, schema, ship


@pytest.fixture
def hub_ready(hub_db, monkeypatch):
    """Initialise a hub DB and wire it into the hub module's globals."""
    conn = core.connect(str(hub_db), check_same_thread=False)
    schema.init_hub(conn)
    ro_conn = core.connect_ro(str(hub_db))  # GET endpoints read through this (mirrors main())
    monkeypatch.setattr(hub, "_conn", conn)
    monkeypatch.setattr(hub, "_ro_conn", ro_conn)
    hub._hub_cols.clear()
    hub._hub_cols.update({
        t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        for t in schema.STD_TABLES
    })
    yield conn
    conn.close()
    ro_conn.close()


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


def test_ingest_records_live_rate_buffer(hub_ready):
    """Each accepted ingest appends (ts, wire, raw, rows) to the bounded in-memory ring buffer
    that backs /api/ingest-rate - not the SQLite DB. The snapshot + ingest_rate() see it."""
    hub._ingest_events.clear()
    ts0 = time.time()
    counts = hub.ingest(_payload(ts0), wire_bytes=1234, raw_bytes=5678)
    snap = hub._ingest_snapshot()
    assert len(snap) == 1
    ts, wire, raw, rows = snap[0]
    assert wire == 1234 and raw == 5678 and rows == sum(counts.values())
    rate = hubapi.ingest_rate(snap)
    assert rate["total_wire_bytes"] == 1234 and rate["posts"] == 1 and rate["last_ts"] == ts
    hub._ingest_events.clear()


def test_ingest_buffer_drops_aged_out_events(hub_ready):
    """_ingest_snapshot trims events older than the rate window so memory tracks the live window."""
    hub._ingest_events.clear()
    now = time.time()
    hub._record_ingest(now - hub._INGEST_RATE_WINDOW_S - 100, 10, 10, 1)  # stale
    hub._record_ingest(now - 10, 20, 20, 2)                               # fresh
    snap = hub._ingest_snapshot(now=now)
    assert len(snap) == 1 and snap[0][1] == 20
    assert len(hub._ingest_events) == 1  # deque compacted in place
    hub._ingest_events.clear()


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
    payload, maxids = ship.gather(conn, "d")
    assert "ping_runs" in payload
    assert "ping_rtts" not in payload
    assert "ping_rtts" not in maxids


def test_rtts_shipped_when_opted_in(node_conn, monkeypatch):
    """SHIP_RTTS=1: raw rtts ship, capped to already-gathered ping_runs."""
    monkeypatch.setattr(config, "SHIP_RTTS", True)
    conn, rid = node_conn
    payload, maxids = ship.gather(conn, "d")
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


def test_favicon_served_not_404(hub_ready):
    """Both /favicon.svg and the browser's implicit /favicon.ico return the brand sparkline (200),
    so the dashboard no longer logs a 404 for the tab icon."""
    srv = core_http_server()
    try:
        port = srv.server_address[1]
        for path in ("/favicon.svg", "/favicon.ico"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
                assert resp.status == 200
                assert resp.headers["Content-Type"] == "image/svg+xml"
                body = resp.read()
                assert body.startswith(b"<svg") and b"#58a6ff" in body  # the brand-blue sparkline
    finally:
        srv.shutdown()
        srv.server_close()


def test_dashboard_links_favicon():
    assert 'rel="icon"' in hubapi.dashboard_html() and "/favicon.svg" in hubapi.dashboard_html()
    assert hubapi.FAVICON_SVG.startswith(b"<svg")


def test_dashboard_has_loading_warmup():
    """The cache-backed tabs get a first-open warm-up screen (spinner + explanation) instead
    of a grey blank while the server populates its cache."""
    h = hubapi.dashboard_html()
    assert 'class="loading"' in h and "function loadingHtml" in h
    assert "warms the hub's cache" in h
    assert "const WARMUP=" in h
    # each heavy, cache-backed view names its warm-up
    for hint in ("the 24-hour ranking", "the latency heatmap", "the measured ship-cost view"):
        assert hint in h


# --- security hardening: ingest auth fails closed, decompression is bounded, hours is clamped ---

def _ingest_post(port, body, headers):
    return urllib.request.Request(
        f"http://127.0.0.1:{port}/ingest", data=body, method="POST", headers=headers)


def test_ingest_fails_closed_without_secret(hub_ready, monkeypatch):
    """An empty/unset HUB_SECRET must reject ingest (503), not accept unauthenticated pushes
    (hmac.compare_digest('', '') is True, so the old code authorized everyone)."""
    monkeypatch.setattr(config, "HUB_SECRET", "")
    assert hub._ingest_secret() is None
    monkeypatch.setattr(config, "HUB_SECRET", "changeme")  # the install default is also "no secret"
    assert hub._ingest_secret() is None

    srv = core_http_server()
    try:
        port = srv.server_address[1]
        body = json.dumps(_payload(time.time())).encode()
        req = _ingest_post(port, body, {"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 503
    finally:
        srv.shutdown()
        srv.server_close()
    assert hub_ready.execute("SELECT COUNT(*) FROM ping_runs").fetchone()[0] == 0


def test_ingest_wrong_key_401_correct_key_200(hub_ready, monkeypatch):
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    srv = core_http_server()
    try:
        port = srv.server_address[1]
        body = json.dumps(_payload(time.time())).encode()
        wrong = _ingest_post(port, body, {"Content-Type": "application/json", "X-Smokemon-Key": "nope"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(wrong, timeout=5)
        assert ei.value.code == 401

        ok = _ingest_post(port, body, {"Content-Type": "application/json", "X-Smokemon-Key": "s3cret"})
        with urllib.request.urlopen(ok, timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert hub_ready.execute("SELECT COUNT(*) FROM ping_runs").fetchone()[0] == 2


def test_gunzip_bounded_caps_output():
    raw = b"x" * 50000
    packed = gzip.compress(raw)
    assert hub._gunzip_bounded(packed, 100000) == raw  # under the cap: exact roundtrip
    with pytest.raises(ValueError):
        hub._gunzip_bounded(packed, 1000)  # over the cap: rejected, no unbounded allocation


def test_gzip_bomb_returns_413_not_500(hub_ready, monkeypatch):
    """A tiny gzip body that inflates well past HUB_MAX_BODY must be rejected with a clean 413,
    never decompressed into RAM (no OOM, no 500 traceback)."""
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    monkeypatch.setattr(config, "HUB_MAX_BODY", 4096)
    body = gzip.compress(b"a" * 500000)  # ~120 compressed bytes -> inflates to 500 KB
    assert len(body) <= config.HUB_MAX_BODY  # passes the compressed-length gate first
    srv = core_http_server()
    try:
        port = srv.server_address[1]
        req = _ingest_post(port, body, {"Content-Type": "application/json",
                                        "Content-Encoding": "gzip", "X-Smokemon-Key": "s3cret"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 413
    finally:
        srv.shutdown()
        srv.server_close()


def test_clamp_hours_bounds_and_defaults():
    assert hub._clamp_hours({"hours": ["1e9"]}) == hub._MAX_HOURS  # huge value clamped
    assert hub._clamp_hours({"hours": ["-5"]}) == 0.0              # negative floored
    assert hub._clamp_hours({"hours": ["abc"]}) == 24.0           # non-numeric -> default
    assert hub._clamp_hours({}) == 24.0
    assert hub._clamp_hours({}, default=2.0) == 2.0
    assert hub._clamp_hours({"hours": ["6"]}) == 6.0


def test_huge_hours_query_does_not_500(hub_ready):
    """An attacker-supplied ?hours=1e9 (or garbage) must not throw into the 500 path."""
    srv = core_http_server()
    try:
        port = srv.server_address[1]
        for q in ("hours=1e9", "hours=notanumber"):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fleet?{q}", timeout=5) as resp:
                assert resp.status == 200
    finally:
        srv.shutdown()
        srv.server_close()
