"""Central ingest server (runs on the hub). Receives delta batches from nodes' shipper
via POST /ingest and writes a hub DB (same schema + node + src_id). Idempotent via
UNIQUE(node,src_id) + INSERT OR IGNORE in one transaction. Stdlib http.server."""

import collections
import hmac
import json
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import alerts, config, core, hubapi, notify, schema

_conn = None              # writer connection: ingest + housekeeping, guarded by _lock
_lock = threading.Lock()  # serialize writes to the single sqlite connection
_ro_conn = None           # read-only connection: dashboard/API GETs, guarded by _read_lock
_read_lock = threading.Lock()  # serialize reads among themselves; WAL lets them run beside ingest
# Bounded number of in-flight HTTP requests so a spike cannot exhaust threads/RSS.
_MAX_CONCURRENT = 100
_request_sem = threading.Semaphore(_MAX_CONCURRENT)
# Short-TTL memoization for the expensive aggregate endpoints. Keyed by path+params; a per-key
# lock makes concurrent identical misses collapse into a single recompute (single-flight) so the
# dashboard's polls/tabs/reloads/users share one result instead of each paying seconds.
# Bounded LRU, because part of every key comes from the query string: `node` is free-form text
# and an unbounded dict would let anyone grow the hub's RSS without limit by walking node names.
_RESP_CACHE_MAX = 256
_resp_cache: collections.OrderedDict[str, tuple[float, object]] = collections.OrderedDict()
_resp_cache_locks: dict[str, threading.Lock] = {}
_resp_meta_lock = threading.Lock()
_hub_cols: dict[str, set[str]] = {}
_last_prune = 0.0  # ingest_log housekeeping throttle (prune at most hourly)
_INGEST_LOG_RETENTION_S = 14 * 86400

# Live ingest-rate gauge: a bounded in-memory ring buffer of (ts, wire_bytes, raw_bytes, rows)
# for each accepted POST /ingest. Deliberately NOT persisted (the durable per-node ship cost is
# already in ingest_log) - this is cheap, ephemeral, and only powers the realtime dashboard gauge.
# maxlen caps memory; snapshots also drop anything older than the rate window so it stays flat.
_INGEST_RATE_WINDOW_S = 900.0  # 15 min horizon backing the live gauge sparkline
_INGEST_BUF_MAX = 8000  # hard cap on retained ingest events (~0.5 MB worst case)
_ingest_events: collections.deque = collections.deque(maxlen=_INGEST_BUF_MAX)
_ingest_buf_lock = threading.Lock()


def _record_ingest(ts: float, wire_bytes: int, raw_bytes: int, rows: int) -> None:
    """Append one accepted ingest to the bounded in-memory ring buffer behind /api/ingest-rate."""
    with _ingest_buf_lock:
        _ingest_events.append((ts, int(wire_bytes), int(raw_bytes), int(rows)))


def _ingest_snapshot(now: float | None = None) -> list[tuple[float, int, int, int]]:
    """Copy of the ring buffer trimmed to the rate window, compacting the deque so retained
    memory tracks the live window even when traffic is sparse."""
    now = time.time() if now is None else now
    cutoff = now - _INGEST_RATE_WINDOW_S
    with _ingest_buf_lock:
        events = [e for e in _ingest_events if e[0] >= cutoff]
        if len(events) != len(_ingest_events):  # drop aged-out events from the live buffer
            _ingest_events.clear()
            _ingest_events.extend(events)
        return events


def _insert_std(conn, table, node, columns, rows) -> int:
    """Generic INSERT OR IGNORE; id -> src_id, node taken from the row's own node (with
    payload node as fallback).

    Runs a single executemany() (much faster on Pi-class hardware) and returns the number of
    rows actually inserted (computed via total_changes delta around the call)."""
    cols = _hub_cols[table]
    body = [c for c in columns if c not in ("id", "node") and c in cols]
    idx = [columns.index(c) for c in body]
    id_i = columns.index("id")
    node_i = columns.index("node") if "node" in columns else None
    insert_cols = body + ["node", "src_id"]
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(insert_cols)}) VALUES ({','.join('?' * len(insert_cols))})"

    def _row_args(r):
        row_node = r[node_i] if node_i is not None and r[node_i] is not None else node
        return [r[i] for i in idx] + [row_node, r[id_i]]

    before = conn.total_changes
    conn.executemany(sql, (_row_args(r) for r in rows))
    return conn.total_changes - before


def ingest(payload: dict, wire_bytes: int = 0, raw_bytes: int = 0) -> dict:
    global _last_prune
    node, tables = payload["node"], payload["tables"]
    counts: dict[str, int] = {}
    with _lock:
        try:
            _conn.execute("BEGIN")
            for table in schema.STD_TABLES:
                if table not in tables:
                    continue
                t = tables[table]
                counts[table] = _insert_std(_conn, table, node, t["columns"], t["rows"])
            # record actual wire cost of this push (measured, not estimated) for the cost view
            now = time.time()
            _conn.execute("INSERT INTO ingest_log (ts, node, wire_bytes, raw_bytes, rows) VALUES (?,?,?,?,?)",
                          (now, node, wire_bytes, raw_bytes, sum(counts.values())))
            if now - _last_prune > 3600:  # hourly, in-band under the same lock
                _conn.execute("DELETE FROM ingest_log WHERE ts < ?", (now - _INGEST_LOG_RETENTION_S,))
                _last_prune = now
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
    # feed the ephemeral live-rate gauge (outside the DB lock; cheap deque append)
    _record_ingest(time.time(), wire_bytes, raw_bytes, sum(counts.values()))
    return counts


def _ingest_secret() -> str | None:
    """The ingest secret to authorize against, or None when no real secret is configured.
    Failing closed here prevents the empty/default-secret auth bypass (hmac.compare_digest('','')
    is True), which matters because the hub binds 0.0.0.0 by default."""
    s = config.HUB_SECRET
    return s if s and s != "changeme" else None


def _gunzip_bounded(data: bytes, limit: int) -> bytes:
    """Streaming gzip inflate capped at `limit` bytes, so a small compressed body cannot expand
    into a multi-GB decompression bomb that OOMs the hub. Raises ValueError once output exceeds
    the cap."""
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    for i in range(0, len(data), 65536):
        out += dec.decompress(data[i:i + 65536], max(1, limit - len(out) + 1))
        if len(out) > limit:
            raise ValueError("decompressed body too large")
    out += dec.flush()
    if len(out) > limit:
        raise ValueError("decompressed body too large")
    return bytes(out)


_MAX_HOURS = 24 * 90  # clamp the window so ?hours=1e9 can't drive an unbounded scan
# The window is snapped to this ladder before it is used OR cached. Two reasons: a float taken
# straight from the query string is an unbounded set of cache keys (?hours=1.0000001 ad
# infinitum), and rounding up never shows the caller less data than it asked for.
_HOUR_BUCKETS = (1.0, 6.0, 24.0, 72.0, 168.0, 720.0, float(_MAX_HOURS))


def _clamp_hours(qs: dict, default: float = 24.0) -> float:
    """Parse the `hours` query param defensively: non-numeric input falls back to the default
    instead of throwing into the 500 path, and the value is clamped and snapped to _HOUR_BUCKETS."""
    try:
        hours = float(qs.get("hours", [str(default)])[0])
    except (TypeError, ValueError):
        hours = default
    hours = max(0.0, min(hours, _MAX_HOURS))
    return next(b for b in _HOUR_BUCKETS if b >= hours)


def _clamp_int(qs: dict, name: str, default: int, lo: int, hi: int) -> int:
    """Bounded integer query param. Same contract as _clamp_hours: garbage falls back to the
    default rather than into the 500 path, and the range keeps it out of unbounded-scan territory."""
    try:
        return max(lo, min(int(qs.get(name, [str(default)])[0]), hi))
    except (TypeError, ValueError):
        return default


# A client that reloads, navigates away, or hits its own fetch timeout aborts the in-flight
# request; the next write from our side then raises one of these. It's normal for a polling
# dashboard, not a server fault - swallow it instead of dumping a traceback per cancelled fetch.
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _ro_call(fn):
    """Run a read-only query under the read lock against the read-only connection."""
    with _read_lock:
        return fn(_ro_conn)


def _cached(key: str, producer, ttl: float | None = None):
    """Serve `producer()` memoized for up to `ttl` seconds (default HUB_CACHE_TTL_S). On a miss a
    single thread recomputes while concurrent callers wait on the per-key lock and then get the
    fresh value (single-flight), so the dashboard's repeated/parallel polls don't each pay the full
    recompute. A per-key ttl lets fast-poll endpoints use a SHORTER window (fleet-status/spark) and
    slow-changing ones a LONGER window (the hourly heatmap) than the default. Setting the global
    HUB_CACHE_TTL_S<=0 is the kill-switch: it disables caching for every key regardless of ttl."""
    if config.HUB_CACHE_TTL_S <= 0:  # global off-switch
        return producer()
    ttl = config.HUB_CACHE_TTL_S if ttl is None else ttl
    if ttl <= 0:
        return producer()
    with _resp_meta_lock:
        hit = _resp_cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            _resp_cache.move_to_end(key)
            return hit[1]
        lock = _resp_cache_locks.setdefault(key, threading.Lock())
    with lock:
        with _resp_meta_lock:
            hit = _resp_cache.get(key)  # another thread may have refreshed while we waited
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        val = producer()
        with _resp_meta_lock:
            _resp_cache[key] = (time.time(), val)
            _resp_cache.move_to_end(key)
            _evict_locked()
        return val


def _evict_locked() -> None:
    """Drop least-recently-used entries past the cap. Caller holds _resp_meta_lock.

    The lock dict is evicted alongside the value, otherwise it becomes the unbounded map the cap
    was meant to prevent. Evicting a lock another thread is currently holding is harmless: that
    thread already has its reference, and the worst case is one lost single-flight collapse."""
    while len(_resp_cache) > _RESP_CACHE_MAX:
        old, _ = _resp_cache.popitem(last=False)
        _resp_cache_locks.pop(old, None)


class Handler(BaseHTTPRequestHandler):
    # A peer that opens a connection and then stalls (half-open TCP, a node whose link dropped
    # mid-POST) otherwise holds a ThreadingHTTPServer thread forever; enough of them and the hub
    # stops accepting ingest entirely.
    timeout = 30

    def _write(self, code: int, data: bytes, content_type: str,
               extra: dict | None = None, no_store: bool = False) -> None:
        """Write a complete response, tolerating a client that hung up mid-flight."""
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if no_store:
                self.send_header("Cache-Control", "no-store")  # always serve fresh html/css/png
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)
        except _CLIENT_GONE:
            self.close_connection = True  # client went away; nothing to send, don't raise

    def _send(self, code: int, obj: dict) -> None:
        self._write(code, json.dumps(obj).encode(), "application/json")

    def _send_text(self, code: int, body: str, content_type: str) -> None:
        self._write(code, body.encode(), content_type, no_store=True)

    def _send_bytes(self, code: int, data: bytes, content_type: str, extra: dict | None = None) -> None:
        self._write(code, data, content_type, extra=extra, no_store=True)

    def do_GET(self):  # noqa: N802
        if not _request_sem.acquire(blocking=False):
            return self._send(503, {"error": "server busy"})
        try:
            return self._do_get()
        finally:
            _request_sem.release()

    def _do_get(self) -> None:
        """Read-only S2/S3 surfaces. Reads go through a dedicated read-only connection under
        _read_lock; WAL lets them run concurrently with ingest instead of queuing behind the
        writer's _lock, so the dashboard never competes with intake."""
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        hours = _clamp_hours(qs)
        try:
            if u.path == "/metrics":
                text = _cached("metrics",
                               lambda: _ro_call(hubapi.prometheus),
                               ttl=10.0)
                return self._send_text(200, text, "text/plain; version=0.0.4; charset=utf-8")
            if u.path == "/":
                return self._send_text(200, hubapi.dashboard_html(), "text/html; charset=utf-8")
            if u.path in ("/favicon.svg", "/favicon.ico"):  # tab icon = the header sparkline; no more 404
                return self._send_bytes(200, hubapi.FAVICON_SVG, "image/svg+xml")
            if u.path == "/health":
                return self._send(200, {"ok": True, "service": "smokemon-hub"})
            if u.path == "/api/nodes":
                with _read_lock:
                    return self._send(200, {"nodes": hubapi.nodes(_ro_conn)})
            if u.path == "/api/fleet":
                # short TTL: the live grid polls this every ~5s; caching collapses concurrent
                # viewers/tabs onto one liveness pass without feeling stale.
                data = _cached("fleet", lambda: {"fleet": _ro_call(hubapi.fleet)}, ttl=3.0)
                return self._send(200, data)
            if u.path == "/api/incidents":
                node = qs.get("node", [""])[0]
                min_sev = _clamp_int(qs, "min_severity", 1, 1, 4)
                limit = _clamp_int(qs, "limit", 200, 1, 1000)
                data = _cached(
                    f"incidents:{node}:{min_sev}:{hours}:{limit}",
                    lambda: _ro_call(lambda c: hubapi.incidents_feed(
                        c, hours, node or None, min_sev, limit)))
                return self._send(200, data)
            if u.path == "/api/incident":
                uid = qs.get("uid", [""])[0]
                # Not cached: uid is free-form caller input, so every distinct value would claim
                # a cache slot, and a single incident lookup is cheap enough not to need one.
                with _read_lock:
                    inc = hubapi.incident_detail(_ro_conn, uid)
                if inc is None:
                    return self._send(404, {"error": "no such incident"})
                return self._send(200, inc)
            if u.path == "/api/density":
                # node x hour incident counts: it only changes when an incident opens or closes,
                # so a long TTL (clamped down by HUB_CACHE_TTL_S if that global is set lower)
                # keeps the dashboard's polls off it.
                data = _cached(f"density:{hours}",
                               lambda: _ro_call(lambda c: hubapi.incident_density(c, hours)),
                               ttl=600.0)
                return self._send(200, data)
            if u.path == "/api/logs":
                node = qs.get("node", [""])[0]
                sev = qs.get("severity", ["elevated"])[0]
                if sev not in ("all", "elevated"):
                    sev = "elevated"
                data = _cached(f"logs:{node}:{sev}:{hours}",
                               lambda: _ro_call(lambda c: hubapi.events_log(c, node or None, sev, hours)))
                return self._send(200, data)
            if u.path == "/api/inventory":
                with _read_lock:
                    return self._send(200, hubapi.inventory(_ro_conn))
            if u.path == "/api/hub-health":
                data = _cached("hub-health",
                               lambda: _ro_call(hubapi.hub_health),
                               ttl=10.0)
                return self._send(200, data)
            if u.path == "/api/cost":
                data = _cached(f"cost:{hours}", lambda: _ro_call(lambda c: hubapi.ship_volume(c, hours)))
                return self._send(200, data)
            if u.path == "/api/ingest-rate":
                # in-memory ring buffer, not the DB - no _lock needed (own buffer lock)
                return self._send(200, hubapi.ingest_rate(_ingest_snapshot()))
        except _CLIENT_GONE:
            return None  # client hung up before/while we responded - normal, not an error
        except Exception as e:  # noqa: BLE001
            core.log(f"GET {u.path} error: {e!r}")
            return self._send(500, {"error": "internal error"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if not _request_sem.acquire(blocking=False):
            return self._send(503, {"error": "server busy"})
        try:
            return self._do_post()
        finally:
            _request_sem.release()

    def _do_post(self) -> None:
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        # Fail closed: refuse ingest entirely when no real secret is configured, so an empty/
        # default HUB_SECRET can't accept unauthenticated pushes (hmac.compare_digest('','') is True).
        secret = _ingest_secret()
        if secret is None:
            return self._send(503, {"error": "ingest disabled: server secret not configured"})
        # compare_digest and the Content-Length parse both live inside the try: compare_digest
        # raises TypeError on a non-ASCII header value and int() raises ValueError on a
        # non-numeric one, and either escaping here is an uncaught traceback per request.
        try:
            if not hmac.compare_digest(self.headers.get("X-Smokemon-Key", ""), secret):
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                return self._send(413, {"error": "bad length"})
            if length <= 0 or length > config.HUB_MAX_BODY:
                return self._send(413, {"error": "bad length"})
            compressed = self.rfile.read(length)
            body = compressed
            if "gzip" in self.headers.get("Content-Encoding", "").lower():
                try:  # bounded inflate: a small gzip can't expand into a multi-GB OOM
                    body = _gunzip_bounded(compressed, config.HUB_MAX_BODY)
                except ValueError:
                    return self._send(413, {"error": "decompressed body too large"})
            # wire_bytes = what actually crossed the network (compressed); raw_bytes = decoded size
            counts = ingest(json.loads(body), wire_bytes=len(compressed), raw_bytes=len(body))
        except _CLIENT_GONE:
            return None  # node dropped the connection mid-push - the shipper will retry next drain
        except Exception as e:  # noqa: BLE001
            core.log(f"ingest error: {e!r}")
            return self._send(500, {"error": "internal error"})
        self._send(200, {"ok": True, "counts": counts})

    def log_message(self, *_args):  # silence default access log
        pass


def _alert_loop() -> None:
    """Background alert pass. Reuses the same detector the Risk tab shows; tracks every firing
    alert in alert_state (powering the dashboard's firing-since), and pushes the page-able subset
    (not muted, and only when a notify URL is set) to the webhook. With no NOTIFY_URL this is a
    pure tracker that sends nothing. Locking mirrors the GET path: reads under _read_lock (WAL,
    concurrent with ingest), the small alert_state writes under _lock, and the webhook POST
    outside every lock so a slow/blocked notify endpoint can never stall intake. Never lets an
    exception kill the loop."""
    while True:
        time.sleep(max(5.0, config.ALERT_EVAL_INTERVAL))
        try:
            now = time.time()
            with _read_lock:
                current = alerts.evaluate(_ro_conn, now)
            with _lock:
                state = alerts.load_state(_conn)
            firing, resolved = alerts.plan(current, state, now)
            page_firing = alerts.to_page(firing)
            page_resolved = alerts.to_page(resolved)
            title, body = alerts.render(page_firing, page_resolved)
            sent = notify.send(title, body) if title else False
            notified = {a["key"] for a in page_firing} if sent else set()
            with _lock:  # persist ALL current (tracks firing-since) + drop ALL resolved
                alerts.persist(_conn, current, resolved, notified, now)
        except Exception as e:  # noqa: BLE001
            core.log(f"alert loop error: {e!r}")


def main() -> int:
    global _conn, _ro_conn
    if config.HUB_SECRET == "changeme":
        core.log("WARNING: SMOKEMON_HUB_SECRET is default 'changeme' - set a real secret.")
    _conn = core.connect(config.HUB_DB, check_same_thread=False)
    schema.init_hub(_conn)
    # Dashboard/API reads use a separate read-only connection (opened after the schema exists)
    # so GETs read under WAL without contending on the writer's lock.
    _ro_conn = core.connect_ro(config.HUB_DB)
    _hub_cols.update({t: {r[1] for r in _conn.execute(f"PRAGMA table_info({t})").fetchall()}
                      for t in schema.STD_TABLES})
    if config.ALERT_TRACK or config.NOTIFY_URL:  # background alert pass (track always, page if URL)
        threading.Thread(target=_alert_loop, name="smokemon-alerts", daemon=True).start()
        if config.NOTIFY_URL:
            core.log(f"alert delivery on: every {config.ALERT_EVAL_INTERVAL:.0f}s "
                     f"-> {config.NOTIFY_KIND or notify.detect_kind(config.NOTIFY_URL)} "
                     f"(min severity {config.NOTIFY_MIN_SEVERITY})")
        else:
            core.log(f"alert tracking on: every {config.ALERT_EVAL_INTERVAL:.0f}s "
                     "(delivery off - no SMOKEMON_NOTIFY_URL)")
    srv = ThreadingHTTPServer((config.HUB_BIND, config.HUB_PORT), Handler)
    core.log(f"hub listening on {config.HUB_BIND}:{config.HUB_PORT} db={config.HUB_DB} "
             "(dashboard GET / · POST /ingest · GET /metrics /api/fleet /api/incidents "
             "/api/incident /api/density /api/logs /api/nodes /api/inventory "
             "/api/hub-health /api/cost /api/ingest-rate)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        _conn.close()
        if _ro_conn is not None:
            _ro_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
