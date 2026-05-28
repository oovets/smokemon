#!/usr/bin/env python3
"""smokemon hub ingest — central mottagare (körs på hubben, t.ex. app01). Tar emot
delta-batchar från nodernas shipper.py via POST /ingest och skriver till en hubb-DB
med samma schema som noderna + en 'node'-kolumn och 'src_id' (nodens lokala radid).
Idempotent via UNIQUE(node, src_id) + INSERT OR IGNORE i en transaktion per request.
Ren stdlib (http.server)."""

import hmac
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HOME = os.path.expanduser("~")
DB_PATH = os.environ.get("SMOKEMON_HUB_DB", os.path.join(HOME, "smokemon", "data", "smokemon-hub.db"))
SECRET = os.environ.get("SMOKEMON_HUB_SECRET", "changeme")
BIND = os.environ.get("SMOKEMON_HUB_BIND", "0.0.0.0")
PORT = int(os.environ.get("SMOKEMON_HUB_PORT", "8765"))
MAX_BODY = int(os.environ.get("SMOKEMON_HUB_MAX_BODY", str(64 * 1024 * 1024)))  # 64 MB

# Hub-schema: nodernas kolumner + node + src_id, UNIQUE(node, src_id) för idempotens.
# ping_rtts är specialfall: run_id pekar på HUBBENS ping_runs.id (översätts vid ingest).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ping_runs (
    id INTEGER PRIMARY KEY, ts REAL, target TEXT, sent INTEGER, recv INTEGER, loss_pct REAL,
    rtt_min REAL, rtt_median REAL, rtt_avg REAL, rtt_max REAL, rtt_stddev REAL,
    node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_ping_runs_node_ts ON ping_runs(node, ts);

CREATE TABLE IF NOT EXISTS ping_rtts (
    id INTEGER PRIMARY KEY, run_id INTEGER, rtt_ms REAL
);
CREATE INDEX IF NOT EXISTS ix_ping_rtts_run ON ping_rtts(run_id);

CREATE TABLE IF NOT EXISTS net_samples (
    id INTEGER PRIMARY KEY, ts REAL, iface TEXT, ibytes INTEGER, obytes INTEGER,
    ipkts INTEGER, opkts INTEGER, node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_net_node_ts ON net_samples(node, ts);

CREATE TABLE IF NOT EXISTS http_samples (
    id INTEGER PRIMARY KEY, ts REAL, url TEXT, http_code INTEGER,
    dns_ms REAL, connect_ms REAL, tls_ms REAL, ttfb_ms REAL, total_ms REAL,
    node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_http_node_ts ON http_samples(node, ts);

CREATE TABLE IF NOT EXISTS mtr_hops (
    id INTEGER PRIMARY KEY, ts REAL, target TEXT, hop_no INTEGER, host TEXT,
    loss_pct REAL, sent INTEGER, last_ms REAL, avg_ms REAL, best_ms REAL, worst_ms REAL, stddev_ms REAL,
    node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_mtr_node_ts ON mtr_hops(node, ts);

CREATE TABLE IF NOT EXISTS wifi_samples (
    id INTEGER PRIMARY KEY, ts REAL, ssid TEXT, channel TEXT, phy_mode TEXT,
    rssi_dbm INTEGER, noise_dbm INTEGER, tx_rate_mbps REAL,
    node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_wifi_node_ts ON wifi_samples(node, ts);

CREATE TABLE IF NOT EXISTS iperf_samples (
    id INTEGER PRIMARY KEY, ts REAL, server TEXT, up_mbps REAL, down_mbps REAL, retransmits INTEGER,
    node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_iperf_node_ts ON iperf_samples(node, ts);

CREATE TABLE IF NOT EXISTS host_samples (
    id INTEGER PRIMARY KEY, ts REAL, cpu_pct REAL, load1 REAL, load5 REAL, load15 REAL,
    mem_used_pct REAL, mem_total_mb REAL, temp_c REAL, disk_read_mbps REAL, disk_write_mbps REAL,
    node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_host_node_ts ON host_samples(node, ts);

CREATE TABLE IF NOT EXISTS disk_samples (
    id INTEGER PRIMARY KEY, ts REAL, mount TEXT, used_pct REAL, free_gb REAL,
    node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_disk_node_ts ON disk_samples(node, ts);

CREATE TABLE IF NOT EXISTS proc_samples (
    id INTEGER PRIMARY KEY, ts REAL, pid INTEGER, name TEXT, cpu_pct REAL, rss_mb REAL,
    node TEXT, src_id INTEGER, UNIQUE(node, src_id)
);
CREATE INDEX IF NOT EXISTS ix_proc_node_ts ON proc_samples(node, ts);
"""

# Standardtabeller (alla utom ping_rtts) som ingest:as generiskt.
_STD_TABLES = (
    "ping_runs", "net_samples", "http_samples", "mtr_hops", "wifi_samples",
    "iperf_samples", "host_samples", "disk_samples", "proc_samples",
)

_conn: sqlite3.Connection | None = None
_hub_cols: dict[str, set[str]] = {}


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def init_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_SCHEMA)
    conn.commit()
    for t in _STD_TABLES:
        _hub_cols[t] = {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
    return conn


def _insert_std(conn, table, node, columns, rows) -> dict[int, int]:
    """Generisk INSERT OR IGNORE. 'id' blir src_id; 'node' tas från radens egen node-kolumn
    (det collectorn faktiskt mätte), med payloadens node som fallback för äldre NULL-rader.
    Returnerar {src_id: hub_id} för rader som faktiskt infogades (nya)."""
    hub_cols = _hub_cols[table]
    body_cols = [c for c in columns if c not in ("id", "node") and c in hub_cols]
    col_idx = [columns.index(c) for c in body_cols]
    id_idx = columns.index("id")
    node_idx = columns.index("node") if "node" in columns else None
    insert_cols = body_cols + ["node", "src_id"]
    sql = f"INSERT OR IGNORE INTO {table} ({','.join(insert_cols)}) VALUES ({','.join('?' * len(insert_cols))})"
    new_map: dict[int, int] = {}
    for r in rows:
        row_node = r[node_idx] if node_idx is not None and r[node_idx] is not None else node
        cur = conn.execute(sql, [r[i] for i in col_idx] + [row_node, r[id_idx]])
        if cur.rowcount == 1:
            new_map[r[id_idx]] = cur.lastrowid
    return new_map


def _insert_rtts(conn, columns, rows, run_map: dict[int, int]) -> int:
    """Infoga ping_rtts endast för runs som var NYA i denna request (undviker dubbletter
    vid retries). run_id översätts från nodens lokala id till hubbens ping_runs.id."""
    ri, mi = columns.index("run_id"), columns.index("rtt_ms")
    data = [(run_map[r[ri]], r[mi]) for r in rows if r[ri] in run_map]
    conn.executemany("INSERT INTO ping_rtts (run_id, rtt_ms) VALUES (?,?)", data)
    return len(data)


def ingest(payload: dict) -> dict:
    node = payload["node"]
    tables = payload["tables"]
    counts: dict[str, int] = {}
    assert _conn is not None
    try:
        _conn.execute("BEGIN")
        run_map: dict[int, int] = {}
        # ping_runs först, så ping_rtts kan översätta run_id.
        if "ping_runs" in tables:
            t = tables["ping_runs"]
            run_map = _insert_std(_conn, "ping_runs", node, t["columns"], t["rows"])
            counts["ping_runs"] = len(run_map)
        if "ping_rtts" in tables:
            t = tables["ping_rtts"]
            counts["ping_rtts"] = _insert_rtts(_conn, t["columns"], t["rows"], run_map)
        for table in _STD_TABLES:
            if table == "ping_runs" or table not in tables:
                continue
            t = tables[table]
            counts[table] = len(_insert_std(_conn, table, node, t["columns"], t["rows"]))
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
            self._send(404, {"error": "not found"})
            return
        if not hmac.compare_digest(self.headers.get("X-Smokemon-Key", ""), SECRET):
            self._send(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            self._send(413, {"error": "bad length"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            counts = ingest(payload)
        except Exception as e:  # noqa: BLE001
            log(f"ingest error: {e!r}")
            self._send(500, {"error": str(e)})
            return
        self._send(200, {"ok": True, "counts": counts})

    def log_message(self, *_args):  # tysta default-access-loggen
        pass


def main() -> int:
    global _conn
    if SECRET == "changeme":
        log("VARNING: SMOKEMON_HUB_SECRET är default 'changeme' — sätt en riktig hemlighet.")
    _conn = init_db()
    srv = HTTPServer((BIND, PORT), Handler)
    log(f"hub ingest lyssnar på {BIND}:{PORT} db={DB_PATH}")
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
