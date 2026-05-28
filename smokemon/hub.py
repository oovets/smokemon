"""Central ingest server (runs on the hub). Receives delta batches from nodes' shipper
via POST /ingest and writes a hub DB (same schema + node + src_id). Idempotent via
UNIQUE(node,src_id) + INSERT OR IGNORE in one transaction. Stdlib http.server."""

import hmac
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, core, schema

_conn = None
_lock = threading.Lock()  # serialize writes to the single sqlite connection
_hub_cols: dict[str, set[str]] = {}


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

    def do_POST(self):  # noqa: N802
        if self.path != "/ingest":
            return self._send(404, {"error": "not found"})
        if not hmac.compare_digest(self.headers.get("X-Smokemon-Key", ""), config.HUB_SECRET):
            return self._send(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > config.HUB_MAX_BODY:
            return self._send(413, {"error": "bad length"})
        try:
            counts = ingest(json.loads(self.rfile.read(length)))
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
    core.log(f"hub ingest listening on {config.HUB_BIND}:{config.HUB_PORT} db={config.HUB_DB}")
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
