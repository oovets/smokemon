"""Central ingest server (runs on the hub). Receives delta batches from nodes' shipper
via POST /ingest and writes a hub DB (same schema + node + src_id). Idempotent via
UNIQUE(node,src_id) + INSERT OR IGNORE in one transaction. Stdlib http.server."""

import base64
import collections
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, core, hubapi, schema

_conn = None              # writer connection: ingest + housekeeping, guarded by _lock
_lock = threading.Lock()  # serialize writes to the single sqlite connection
_ro_conn = None           # read-only connection: dashboard/API GETs, guarded by _read_lock
_read_lock = threading.Lock()  # serialize reads among themselves; WAL lets them run beside ingest
_render_lock = threading.Lock()  # serialize the (heavy) PNG subprocess renders
# Short-TTL memoization for the expensive aggregate endpoints. Keyed by path+params; a per-key
# lock makes concurrent identical misses collapse into a single recompute (single-flight) so the
# dashboard's polls/tabs/reloads/users share one result instead of each paying seconds.
_resp_cache: dict[str, tuple[float, object]] = {}
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


def _render_png(node: str, hours: float, panels: str, cols: int) -> tuple[bytes | None, str]:
    """Render a node's panel PNG in a short-lived subprocess, so matplotlib never loads
    into the long-lived hub process (its RSS stays ~20 MB). The child reads the hub DB
    read-only and streams the PNG to stdout. Serialized via _render_lock; returns the
    bytes, or None on no-data / error / timeout."""
    width = round(16.0 / max(1, cols), 1)  # keep total figure ~16in wide regardless of cols
    cmd = [sys.executable, "-m", "smokemon.cli", "png",
           "--db", config.HUB_DB, "--node", node, "--hours", str(hours),
           "--panels", panels, "--width", str(width), "--dpi", "96", "--cols", str(cols),
           "--theme", "dark", "--no-title", "--meta", "--out", "-", "--no-open"]
    with _render_lock:
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:  # noqa: BLE001
            core.log(f"png render failed: {e!r}")
            return None, ""
    if p.returncode != 0 or not p.stdout:
        return None, ""
    meta = ""  # per-panel tooltip metadata, emitted on stderr with a sentinel prefix
    for line in p.stderr.decode("utf-8", "replace").splitlines():
        if line.startswith("SMOKEMON_META "):
            meta = line[len("SMOKEMON_META "):]
            break
    return p.stdout, meta


def _render_tui(node: str, hours: float, panels: str, cols: int, width: int, lines: int) -> str | None:
    """Render the plotext TUI frame (the same braille graphs as `smoke tui`) to an ANSI string
    in a short-lived subprocess, sized via COLUMNS/LINES. Lets the dashboard show the granular
    terminal-style plots next to the PNGs. plotext is light (pure python), so unlike the PNG
    path this needs no render lock."""
    base = [sys.executable, "-m", "smokemon.cli", "tui", "--db", config.HUB_DB, "--node", node,
            "--hours", str(hours), "--panels", panels, "--cols", str(cols), "--no-frame"]
    env = dict(os.environ, COLUMNS=str(width), LINES=str(lines), TERM="xterm-256color")
    # First try with legends; if plotext crashes on a degenerate panel, retry legend-less
    # (still shows every graph) rather than failing the whole frame.
    for extra in ([], ["--no-legend"]):
        try:
            p = subprocess.run(base + extra, capture_output=True, timeout=30, env=env)
        except (subprocess.TimeoutExpired, OSError) as e:  # noqa: BLE001
            core.log(f"tui render failed: {e!r}")
            return None
        if p.returncode == 0 and p.stdout:
            return p.stdout.decode("utf-8", "replace")
    return None


def _insert_std(conn, table, node, columns, rows, need_id_map: bool = False) -> dict[int, int] | int:
    """Generic INSERT OR IGNORE; id -> src_id, node taken from the row's own node (with
    payload node as fallback).

    When need_id_map=True (ping_runs only): runs per-row execute() to capture lastrowid
    and returns {src_id: hub_id} for rows actually inserted.

    Otherwise: runs a single executemany() (much faster on Pi-class hardware) and returns
    the number of rows actually inserted (computed via total_changes delta around the call)."""
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

    if need_id_map:
        new_map: dict[int, int] = {}
        for r in rows:
            cur = conn.execute(sql, _row_args(r))
            if cur.rowcount == 1:
                new_map[r[id_i]] = cur.lastrowid
        return new_map

    before = conn.total_changes
    conn.executemany(sql, (_row_args(r) for r in rows))
    return conn.total_changes - before


def _insert_rtts(conn, columns, rows, run_map: dict[int, int]) -> int:
    """ping_rtts only for runs new in this request (no dupes on retry); run_id remapped."""
    ri, mi = columns.index("run_id"), columns.index("rtt_ms")
    data = [(run_map[r[ri]], r[mi]) for r in rows if r[ri] in run_map]
    conn.executemany("INSERT INTO ping_rtts (run_id, rtt_ms) VALUES (?,?)", data)
    return len(data)


def ingest(payload: dict, wire_bytes: int = 0, raw_bytes: int = 0) -> dict:
    global _last_prune
    node, tables = payload["node"], payload["tables"]
    counts: dict[str, int] = {}
    with _lock:
        try:
            _conn.execute("BEGIN")
            run_map: dict[int, int] = {}
            if "ping_runs" in tables:
                t = tables["ping_runs"]
                run_map = _insert_std(_conn, "ping_runs", node, t["columns"], t["rows"], need_id_map=True)
                counts["ping_runs"] = len(run_map)
            if "ping_rtts" in tables:
                t = tables["ping_rtts"]
                counts["ping_rtts"] = _insert_rtts(_conn, t["columns"], t["rows"], run_map)
            for table in schema.STD_TABLES:
                if table == "ping_runs" or table not in tables:
                    continue
                t = tables[table]
                counts[table] = _insert_std(_conn, table, node, t["columns"], t["rows"])
            # record actual wire cost of this push (measured, not estimated) for the cost view
            now = time.time()
            _conn.execute("INSERT INTO ingest_log (ts, node, wire_bytes, raw_bytes, rows) VALUES (?,?,?,?,?)",
                          (now, node, wire_bytes, raw_bytes, sum(counts.values())))
            if now - _last_prune > 3600:  # hourly housekeeping, in-band under the same lock
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


def _clamp_hours(qs: dict, default: float = 24.0) -> float:
    """Parse the `hours` query param defensively: non-numeric input falls back to the default
    instead of throwing into the 500 path, and the value is clamped to a sane maximum."""
    try:
        hours = float(qs.get("hours", [str(default)])[0])
    except (TypeError, ValueError):
        hours = default
    return max(0.0, min(hours, _MAX_HOURS))


# A client that reloads, navigates away, or hits its own fetch timeout aborts the in-flight
# request; the next write from our side then raises one of these. It's normal for a polling
# dashboard, not a server fault - swallow it instead of dumping a traceback per cancelled fetch.
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def _ro_call(fn):
    """Run a read-only query under the read lock against the read-only connection."""
    with _read_lock:
        return fn(_ro_conn)


def _cached(key: str, producer):
    """Serve `producer()` memoized for up to HUB_CACHE_TTL_S. On a miss a single thread recomputes
    while concurrent callers wait on the per-key lock and then get the fresh value (single-flight),
    so the dashboard's repeated/parallel polls don't each pay the full recompute. TTL<=0 = off."""
    ttl = config.HUB_CACHE_TTL_S
    if ttl <= 0:
        return producer()
    hit = _resp_cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _resp_meta_lock:
        lock = _resp_cache_locks.setdefault(key, threading.Lock())
    with lock:
        hit = _resp_cache.get(key)  # another thread may have refreshed while we waited
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = producer()
        _resp_cache[key] = (time.time(), val)
        return val


class Handler(BaseHTTPRequestHandler):
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
        """Read-only S2/S3 surfaces. Reads go through a dedicated read-only connection under
        _read_lock; WAL lets them run concurrently with ingest instead of queuing behind the
        writer's _lock, so the dashboard never competes with intake."""
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        hours = _clamp_hours(qs)
        try:
            if u.path == "/metrics":
                with _read_lock:
                    text = hubapi.prometheus(_ro_conn)
                return self._send_text(200, text, "text/plain; version=0.0.4; charset=utf-8")
            if u.path == "/":
                return self._send_text(200, hubapi.dashboard_html(), "text/html; charset=utf-8")
            if u.path == "/health":
                return self._send(200, {"ok": True, "service": "smokemon-hub"})
            if u.path == "/api/fleet-status":
                with _read_lock:
                    return self._send(200, hubapi.fleet_status(_ro_conn))
            if u.path == "/api/nodes":
                with _read_lock:
                    return self._send(200, {"nodes": hubapi.nodes(_ro_conn)})
            if u.path == "/api/latest":
                with _read_lock:
                    return self._send(200, hubapi.latest_metrics(_ro_conn))
            if u.path == "/api/fleet":
                data = _cached(f"fleet:{hours}", lambda: {"fleet": _ro_call(lambda c: hubapi.fleet(c, hours))})
                return self._send(200, data)
            if u.path == "/api/heatmap":
                metric = qs.get("metric", ["loss"])[0]
                data = _cached(f"heatmap:{metric}:{hours}",
                               lambda: _ro_call(lambda c: hubapi.heatmap(c, metric, hours)))
                return self._send(200, data)
            if u.path == "/api/spark":
                spark_hours = _clamp_hours(qs, default=2.0)
                with _read_lock:
                    return self._send(200, {"spark": hubapi.sparklines(_ro_conn, spark_hours)})
            if u.path == "/api/risks":
                data = _cached(f"risks:{hours}", lambda: _ro_call(lambda c: hubapi.risks(c, hours)))
                return self._send(200, data)
            if u.path == "/api/cost":
                data = _cached(f"cost:{hours}", lambda: _ro_call(lambda c: hubapi.ship_volume(c, hours)))
                return self._send(200, data)
            if u.path == "/api/services":
                data = _cached("services", lambda: _ro_call(hubapi.services))
                return self._send(200, data)
            if u.path == "/api/ports":
                node = qs.get("node", [""])[0]
                if not node:
                    return self._send(400, {"error": "node required"})
                with _read_lock:
                    return self._send(200, hubapi.ports(_ro_conn, node))
            if u.path == "/api/inventory":
                with _read_lock:
                    return self._send(200, hubapi.inventory(_ro_conn))
            if u.path == "/api/ingest-rate":
                # in-memory ring buffer, not the DB - no _lock needed (own buffer lock)
                return self._send(200, hubapi.ingest_rate(_ingest_snapshot()))
            if u.path == "/api/plot":
                node = qs.get("node", [""])[0]
                if not node:
                    return self._send(400, {"error": "node required"})
                try:
                    cols = max(1, min(4, int(qs.get("cols", ["1"])[0])))
                    width = max(60, min(400, int(qs.get("w", ["140"])[0])))
                    lines = max(16, min(300, int(qs.get("h", ["44"])[0])))
                except ValueError:
                    cols, width, lines = 1, 140, 44
                txt = _render_tui(node, hours, qs.get("panels", ["all"])[0], cols, width, lines)
                if not txt:
                    return self._send(404, {"error": "no data for node/window"})
                return self._send_text(200, txt, "text/plain; charset=utf-8")
            if u.path == "/api/png":
                node = qs.get("node", [""])[0]
                if not node:
                    return self._send(400, {"error": "node required"})
                try:
                    cols = max(1, min(4, int(qs.get("cols", ["2"])[0])))
                except ValueError:
                    cols = 2
                png, meta = _render_png(node, hours, qs.get("panels", ["all"])[0], cols)
                if not png:
                    return self._send(404, {"error": "no data for node/window"})
                # panel tooltips: ship the meta (utf-8 json) base64'd in a header (titles
                # carry °C / -> etc., which aren't header-safe raw).
                extra = {"X-Smokemon-Panels": base64.b64encode(meta.encode()).decode()} if meta else None
                return self._send_bytes(200, png, "image/png", extra)
        except _CLIENT_GONE:
            return None  # client hung up before/while we responded - normal, not an error
        except Exception as e:  # noqa: BLE001
            core.log(f"GET {u.path} error: {e!r}")
            return self._send(500, {"error": "internal error"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        # Fail closed: refuse ingest entirely when no real secret is configured, so an empty/
        # default HUB_SECRET can't accept unauthenticated pushes (hmac.compare_digest('','') is True).
        secret = _ingest_secret()
        if secret is None:
            return self._send(503, {"error": "ingest disabled: server secret not configured"})
        if not hmac.compare_digest(self.headers.get("X-Smokemon-Key", ""), secret):
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > config.HUB_MAX_BODY:
            return self._send(413, {"error": "bad length"})
        try:
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
    srv = ThreadingHTTPServer((config.HUB_BIND, config.HUB_PORT), Handler)
    core.log(f"hub listening on {config.HUB_BIND}:{config.HUB_PORT} db={config.HUB_DB} "
             "(dashboard GET / · POST /ingest · GET /metrics /api/fleet-status "
             "/api/latest /api/fleet /api/heatmap /api/nodes /api/services "
             "/api/ports /api/inventory /api/ingest-rate /api/png)")
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
