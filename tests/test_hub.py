"""Hub ingest and the HTTP surface.

Ingest: first POST inserts, identical replay inserts zero (idempotent via UNIQUE(node, src_id)),
partial overlap inserts only the new rows. Plus the hardening around it -- auth fails closed,
decompression is bounded, and malformed request metadata produces a status code rather than a
traceback per request.
"""

import gzip
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from smokemon import config, core, hub, hubapi, schema

NOW = 1_000_000.0


@pytest.fixture
def hub_ready(hub_db, monkeypatch):
    """Initialise a hub DB and wire it into the hub module's globals."""
    conn = core.connect(str(hub_db), check_same_thread=False)
    schema.init_hub(conn)
    ro_conn = core.connect_ro(str(hub_db))   # GET endpoints read through this (mirrors main())
    monkeypatch.setattr(hub, "_conn", conn)
    monkeypatch.setattr(hub, "_ro_conn", ro_conn)
    monkeypatch.setattr(config, "HUB_CACHE_TTL_S", 0)   # no cross-test cache bleed
    hub._resp_cache.clear()
    hub._resp_cache_locks.clear()
    hub._hub_cols.clear()
    hub._hub_cols.update({
        t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        for t in schema.STD_TABLES
    })
    yield conn
    conn.close()
    ro_conn.close()


def _payload(ts0, node="testnode"):
    """The shape the shipper sends: transitions, their evidence, and a heartbeat."""
    return {
        "node": node,
        "tables": {
            "incidents": {
                "columns": ["id", "ts", "uid", "transition", "signal", "entity", "severity",
                            "worst_value", "opened_ts", "duration_s", "node"],
                "rows": [
                    [1, ts0, "uid-a", "open", "ping.loss", "1.1.1.1", "crit", 90.0, ts0, None, node],
                    [2, ts0 + 60, "uid-a", "close", "ping.loss", "1.1.1.1", "info", None,
                     ts0, 60.0, node],
                ],
            },
            "incident_samples": {
                "columns": ["id", "ts", "uid", "phase", "signal", "entity", "value", "node"],
                "rows": [
                    [10, ts0 - 10, "uid-a", "pre", "ping.loss", "1.1.1.1", 0.0, node],
                    [11, ts0 + 10, "uid-a", "during", "ping.loss", "1.1.1.1", 90.0, node],
                ],
            },
            "heartbeats": {
                "columns": ["id", "ts", "interval_s", "uptime_s", "rss_mb", "cpu_pct", "node"],
                "rows": [[20, ts0, 300.0, 5000.0, 18.0, 1.5, node]],
            },
        },
    }


def _server():
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, r.read(), r.headers


# ---------- ingest ----------

def test_first_ingest(hub_ready):
    counts = hub.ingest(_payload(NOW))
    assert counts["incidents"] == 2
    assert counts["incident_samples"] == 2
    assert counts["heartbeats"] == 1


def test_idempotent_replay(hub_ready):
    payload = _payload(NOW)
    hub.ingest(payload)
    assert all(v == 0 for v in hub.ingest(payload).values())


def test_partial_overlap_inserts_only_new(hub_ready):
    payload = _payload(NOW)
    hub.ingest(payload)
    payload["tables"]["incidents"]["rows"].append(
        [3, NOW + 120, "uid-b", "open", "host.mem", None, "warn", 91.0, NOW + 120, None, "testnode"])
    counts = hub.ingest(payload)
    assert counts["incidents"] == 1
    assert counts["incident_samples"] == 0 and counts["heartbeats"] == 0


def test_ingested_rows_reduce_to_one_incident(hub_ready):
    """The two transitions are a log, not two incidents: the read layer reduces them per uid."""
    hub.ingest(_payload(NOW))
    feed = hubapi.incidents_feed(hub_ready, hours=24, now=NOW + 200)
    assert feed["counts"]["total"] == 1
    inc = feed["incidents"][0]
    assert inc["uid"] == "uid-a" and inc["state"] == "closed"
    assert inc["severity"] == "crit"        # from the open row, not the info-severity close
    assert inc["duration_s"] == 60.0


def test_unknown_table_in_payload_is_ignored(hub_ready):
    """A node running a newer build must not be able to 500 the hub with a table it does not
    know about; ingest only walks the tables it has schema for."""
    payload = _payload(NOW)
    payload["tables"]["from_the_future"] = {"columns": ["id", "ts"], "rows": [[1, NOW]]}
    counts = hub.ingest(payload)
    assert "from_the_future" not in counts and counts["incidents"] == 2


def test_ingest_records_live_rate_buffer(hub_ready):
    """Each accepted ingest appends to the bounded in-memory ring behind /api/ingest-rate --
    not to SQLite."""
    hub._ingest_events.clear()
    counts = hub.ingest(_payload(NOW), wire_bytes=1234, raw_bytes=5678)
    snap = hub._ingest_snapshot()
    assert len(snap) == 1
    _ts, wire, raw, rows = snap[0]
    assert wire == 1234 and raw == 5678 and rows == sum(counts.values())
    assert hubapi.ingest_rate(snap)["posts"] == 1
    hub._ingest_events.clear()


def test_ingest_buffer_drops_aged_out_events(hub_ready):
    hub._ingest_events.clear()
    now = time.time()
    hub._record_ingest(now - hub._INGEST_RATE_WINDOW_S - 100, 10, 10, 1)   # stale
    hub._record_ingest(now - 10, 20, 20, 2)                               # fresh
    snap = hub._ingest_snapshot(now=now)
    assert len(snap) == 1 and snap[0][1] == 20
    assert len(hub._ingest_events) == 1     # deque compacted in place
    hub._ingest_events.clear()


def test_ingest_log_records_measured_wire_cost(hub_ready):
    hub.ingest(_payload(NOW), wire_bytes=999, raw_bytes=4000)
    row = hub_ready.execute("SELECT node, wire_bytes, raw_bytes FROM ingest_log").fetchone()
    assert row == ("testnode", 999, 4000)


# ---------- HTTP: the read routes ----------

def test_every_read_route_answers(hub_ready):
    hub.ingest(_payload(time.time() - 60))
    srv = _server()
    try:
        port = srv.server_address[1]
        for path in ("/api/fleet", "/api/incidents", "/api/density", "/api/logs",
                     "/api/inventory", "/api/hub-health", "/api/cost", "/api/ingest-rate",
                     "/api/nodes"):
            status, body, _ = _get(port, path)
            assert status == 200, path
            json.loads(body)                 # every one of them is valid JSON
        status, body, hdrs = _get(port, "/metrics")
        assert status == 200 and hdrs["Content-Type"].startswith("text/plain")
        assert b"smokemon_node_live" in body
        status, body, hdrs = _get(port, "/")
        assert status == 200 and hdrs["Content-Type"].startswith("text/html")
        status, body, _ = _get(port, "/health")
        assert status == 200 and json.loads(body)["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_deleted_routes_are_gone(hub_ready):
    """The old sample-series surfaces went with the tables behind them; they must 404 rather
    than 500 on a missing hubapi attribute."""
    srv = _server()
    try:
        port = srv.server_address[1]
        for path in ("/api/fleet-status", "/api/nodes-detail", "/api/latest", "/api/heatmap",
                     "/api/spark", "/api/risks", "/api/services", "/api/ports", "/api/series",
                     "/api/network", "/api/plot", "/api/png"):
            with pytest.raises(urllib.error.HTTPError) as ei:
                _get(port, path)
            assert ei.value.code == 404, path
    finally:
        srv.shutdown()
        srv.server_close()


def test_incident_detail_route_404s_for_an_unknown_uid(hub_ready):
    hub.ingest(_payload(time.time() - 60))
    srv = _server()
    try:
        port = srv.server_address[1]
        status, body, _ = _get(port, "/api/incident?uid=uid-a")
        assert status == 200
        d = json.loads(body)
        assert d["uid"] == "uid-a" and len(d["samples"]) == 2

        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/api/incident?uid=nope")
        assert ei.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as ei:
            _get(port, "/api/incident")      # missing param, same answer, no traceback
        assert ei.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_incidents_route_passes_filters_through(hub_ready):
    now = time.time()
    schema.insert(hub_ready, "heartbeats", [{"ts": now - 10, "interval_s": 300.0}], node="pi9")
    schema.insert(hub_ready, "incidents",
                  [{"ts": now - 100, "uid": "u-warn", "transition": "open", "signal": "s",
                    "severity": "warn", "opened_ts": now - 100}], node="pi9")
    hub_ready.commit()
    srv = _server()
    try:
        port = srv.server_address[1]
        assert json.loads(_get(port, "/api/incidents?node=pi9")[1])["counts"]["total"] == 1
        assert json.loads(_get(port, "/api/incidents?node=nobody")[1])["counts"]["total"] == 0
        # gated out by severity
        assert json.loads(_get(port, "/api/incidents?min_severity=4")[1])["counts"]["total"] == 0
        # garbage params fall back instead of 500ing
        for q in ("min_severity=abc", "limit=abc", "limit=-9", "hours=notanumber", "hours=1e9"):
            assert _get(port, f"/api/incidents?{q}")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_logs_route_falls_back_on_an_unknown_severity(hub_ready):
    now = time.time()
    schema.insert(hub_ready, "ext_events", [{"ts": now - 10, "source": "gov", "severity": "error",
                                             "event": "boom", "detail": "d"}], node="pi9")
    hub_ready.commit()
    srv = _server()
    try:
        port = srv.server_address[1]
        d = json.loads(_get(port, "/api/logs?severity=bogus&node=pi9")[1])
        assert [e["event"] for e in d["events"]] == ["boom"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_favicon_served_not_404(hub_ready):
    srv = _server()
    try:
        port = srv.server_address[1]
        for path in ("/favicon.svg", "/favicon.ico"):
            status, body, hdrs = _get(port, path)
            assert status == 200 and hdrs["Content-Type"] == "image/svg+xml"
            assert body.startswith(b"<svg") and b"#58a6ff" in body
    finally:
        srv.shutdown()
        srv.server_close()


# ---------- security hardening ----------

def _post(port, body, headers):
    return urllib.request.Request(
        f"http://127.0.0.1:{port}/ingest", data=body, method="POST", headers=headers)


def test_ingest_fails_closed_without_secret(hub_ready, monkeypatch):
    """An empty/default HUB_SECRET must reject ingest (503), not accept unauthenticated pushes
    (hmac.compare_digest('', '') is True, so the old code authorized everyone)."""
    monkeypatch.setattr(config, "HUB_SECRET", "")
    assert hub._ingest_secret() is None
    monkeypatch.setattr(config, "HUB_SECRET", "changeme")   # the install default is also "no secret"
    assert hub._ingest_secret() is None

    srv = _server()
    try:
        port = srv.server_address[1]
        req = _post(port, json.dumps(_payload(NOW)).encode(),
                    {"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 503
    finally:
        srv.shutdown()
        srv.server_close()
    assert hub_ready.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0


def test_ingest_wrong_key_401_correct_key_200(hub_ready, monkeypatch):
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    srv = _server()
    try:
        port = srv.server_address[1]
        body = json.dumps(_payload(NOW)).encode()
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(
                _post(port, body, {"Content-Type": "application/json",
                                   "X-Smokemon-Key": "nope"}), timeout=5)
        assert ei.value.code == 401

        with urllib.request.urlopen(
                _post(port, body, {"Content-Type": "application/json",
                                   "X-Smokemon-Key": "s3cret"}), timeout=5) as resp:
            assert resp.status == 200 and json.loads(resp.read())["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert hub_ready.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 2


def test_non_ascii_key_is_401_not_a_traceback(hub_ready, monkeypatch):
    """compare_digest raises TypeError on a non-ASCII str. Outside the try that escaped as an
    uncaught traceback per request."""
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    srv = _server()
    try:
        port = srv.server_address[1]
        req = _post(port, json.dumps(_payload(NOW)).encode(),
                    {"Content-Type": "application/json"})
        req.add_header("X-Smokemon-Key", "nyckelträd")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code in (401, 500)          # answered, not hung or crashed
    finally:
        srv.shutdown()
        srv.server_close()


def test_non_numeric_content_length_is_413_not_a_traceback(hub_ready, monkeypatch):
    """int('banana') raised ValueError before the try block. Driven over a raw socket because
    urllib will not send a malformed Content-Length."""
    import socket
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    srv = _server()
    try:
        port = srv.server_address[1]
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"POST /ingest HTTP/1.1\r\nHost: x\r\nX-Smokemon-Key: s3cret\r\n"
                  b"Content-Length: banana\r\n\r\n")
        status = s.recv(64).split(b" ")[1]
        s.close()
        assert status == b"413"
    finally:
        srv.shutdown()
        srv.server_close()


def test_handler_has_a_socket_timeout():
    """A peer that connects and then stalls otherwise holds a server thread forever, and enough
    of them stop the hub accepting ingest at all."""
    assert hub.Handler.timeout == 30


def test_gunzip_bounded_caps_output():
    raw = b"x" * 50000
    packed = gzip.compress(raw)
    assert hub._gunzip_bounded(packed, 100000) == raw
    with pytest.raises(ValueError):
        hub._gunzip_bounded(packed, 1000)


def test_gzip_ingest_roundtrip(hub_ready, monkeypatch):
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    srv = _server()
    try:
        port = srv.server_address[1]
        body = gzip.compress(json.dumps(_payload(NOW)).encode())
        req = _post(port, body, {"Content-Type": "application/json",
                                 "Content-Encoding": "gzip", "X-Smokemon-Key": "s3cret"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200 and json.loads(resp.read())["ok"] is True
    finally:
        srv.shutdown()
        srv.server_close()
    assert hub_ready.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 2


def test_gzip_bomb_returns_413_not_500(hub_ready, monkeypatch):
    """A tiny gzip body that inflates past HUB_MAX_BODY is rejected with a clean 413, never
    decompressed into RAM."""
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    monkeypatch.setattr(config, "HUB_MAX_BODY", 4096)
    body = gzip.compress(b"a" * 500000)
    assert len(body) <= config.HUB_MAX_BODY          # passes the compressed-length gate first
    srv = _server()
    try:
        port = srv.server_address[1]
        req = _post(port, body, {"Content-Type": "application/json",
                                 "Content-Encoding": "gzip", "X-Smokemon-Key": "s3cret"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 413
    finally:
        srv.shutdown()
        srv.server_close()


def test_post_to_an_unknown_path_404s(hub_ready, monkeypatch):
    monkeypatch.setattr(config, "HUB_SECRET", "s3cret")
    srv = _server()
    try:
        port = srv.server_address[1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/nope", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=5)
        assert ei.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
