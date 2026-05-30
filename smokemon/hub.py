"""Central ingest server (runs on the hub). Receives delta batches from nodes' shipper
via POST /ingest and writes a hub DB (same schema + node + src_id). Idempotent via
UNIQUE(node,src_id) + INSERT OR IGNORE in one transaction. Stdlib http.server."""

import base64
import gzip
import hmac
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, core, hubapi, schema

_conn = None
_lock = threading.Lock()  # serialize writes to the single sqlite connection
_render_lock = threading.Lock()  # serialize the (heavy) PNG subprocess renders
_hub_cols: dict[str, set[str]] = {}


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


def ingest(payload: dict) -> dict:
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
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
    return counts


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, code: int, body: str, content_type: str) -> None:
        self._send_bytes(code, body.encode(), content_type)

    def _send_bytes(self, code: int, data: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")  # always serve fresh html/css/png
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        """Read-only S2/S3 surfaces. The hub shares one sqlite connection across threads,
        so every read takes the same write lock the ingest path uses."""
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        hours = float(qs.get("hours", ["24"])[0])
        try:
            if u.path == "/metrics":
                with _lock:
                    text = hubapi.prometheus(_conn)
                return self._send_text(200, text, "text/plain; version=0.0.4; charset=utf-8")
            if u.path == "/":
                return self._send_text(200, hubapi.dashboard_html(), "text/html; charset=utf-8")
            if u.path == "/health":
                return self._send(200, {"ok": True, "service": "smokemon-hub"})
            if u.path == "/api/fleet-status":
                with _lock:
                    return self._send(200, hubapi.fleet_status(_conn))
            if u.path == "/api/nodes":
                with _lock:
                    return self._send(200, {"nodes": hubapi.nodes(_conn)})
            if u.path == "/api/latest":
                with _lock:
                    return self._send(200, hubapi.latest_metrics(_conn))
            if u.path == "/api/fleet":
                with _lock:
                    return self._send(200, {"fleet": hubapi.fleet(_conn, hours)})
            if u.path == "/api/heatmap":
                metric = qs.get("metric", ["loss"])[0]
                with _lock:
                    return self._send(200, hubapi.heatmap(_conn, metric, hours))
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
        except Exception as e:  # noqa: BLE001
            core.log(f"GET {u.path} error: {e!r}")
            return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        if not hmac.compare_digest(self.headers.get("X-Smokemon-Key", ""), config.HUB_SECRET):
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > config.HUB_MAX_BODY:
            return self._send(413, {"error": "bad length"})
        try:
            raw = self.rfile.read(length)
            if "gzip" in self.headers.get("Content-Encoding", "").lower():
                raw = gzip.decompress(raw)  # ship._post gzips the body; plain json still works
            counts = ingest(json.loads(raw))
        except Exception as e:  # noqa: BLE001
            core.log(f"ingest error: {e!r}")
            return self._send(500, {"error": str(e)})
        self._send(200, {"ok": True, "counts": counts})

    def log_message(self, *_args):  # silence default access log
        pass


def main() -> int:
    global _conn
    if config.HUB_SECRET == "changeme":
        core.log("WARNING: SMOKEMON_HUB_SECRET is default 'changeme' - set a real secret.")
    _conn = core.connect(config.HUB_DB, check_same_thread=False)
    schema.init_hub(_conn)
    _hub_cols.update({t: {r[1] for r in _conn.execute(f"PRAGMA table_info({t})").fetchall()}
                      for t in schema.STD_TABLES})
    srv = ThreadingHTTPServer((config.HUB_BIND, config.HUB_PORT), Handler)
    core.log(f"hub listening on {config.HUB_BIND}:{config.HUB_PORT} db={config.HUB_DB} "
             "(dashboard GET / · POST /ingest · GET /metrics /api/fleet-status "
             "/api/latest /api/fleet /api/heatmap /api/nodes /api/png)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        _conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
