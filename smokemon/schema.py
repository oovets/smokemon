"""Single source of truth for the SQLite schema (node-side and hub-side).

Each table's body columns are declared once; node DDL, hub DDL (adds node + src_id +
UNIQUE for idempotent ingest), STD_TABLES and the generic INSERT all derive from it.
ping_rtts is the one special case (no per-row node; run_id references ping_runs)."""

import sqlite3

from . import config

# Body = everything except `id INTEGER PRIMARY KEY` and the trailing node/src_id.
# rtt_p25/p75 are pre-aggregated at collect time so `load_ping_smoke` does not have to
# scan ping_rtts for percentile rendering. Old rows have them NULL; the renderer falls
# back to scanning ping_rtts only for those rows.
_BODY = {
    "ping_runs": "ts REAL NOT NULL, target TEXT NOT NULL, sent INTEGER, recv INTEGER, loss_pct REAL, "
                 "rtt_min REAL, rtt_p25 REAL, rtt_median REAL, rtt_p75 REAL, rtt_avg REAL, rtt_max REAL, "
                 "rtt_stddev REAL",
    "net_samples": "ts REAL NOT NULL, iface TEXT NOT NULL, ibytes INTEGER, obytes INTEGER, ipkts INTEGER, opkts INTEGER",
    "http_samples": "ts REAL NOT NULL, url TEXT NOT NULL, http_code INTEGER, dns_ms REAL, connect_ms REAL, "
                    "tls_ms REAL, ttfb_ms REAL, total_ms REAL",
    "mtr_hops": "ts REAL NOT NULL, target TEXT NOT NULL, hop_no INTEGER, host TEXT, loss_pct REAL, sent INTEGER, "
                "last_ms REAL, avg_ms REAL, best_ms REAL, worst_ms REAL, stddev_ms REAL",
    "wifi_samples": "ts REAL NOT NULL, ssid TEXT, channel TEXT, phy_mode TEXT, "
                    "rssi_dbm INTEGER, noise_dbm INTEGER, tx_rate_mbps REAL, "
                    "bssid TEXT, retry_count INTEGER, discard_count INTEGER, beacon_loss INTEGER",
    "iperf_samples": "ts REAL NOT NULL, server TEXT, up_mbps REAL, down_mbps REAL, retransmits INTEGER, "
                     "rtt_under_load_ms REAL",
    "host_samples": "ts REAL NOT NULL, cpu_pct REAL, load1 REAL, load5 REAL, load15 REAL, mem_used_pct REAL, "
                    "mem_total_mb REAL, temp_c REAL, disk_read_mbps REAL, disk_write_mbps REAL, "
                    "swap_used_pct REAL, cache_mb REAL, oom_kill_count INTEGER, "
                    "psi_cpu REAL, psi_mem REAL, psi_io REAL, "
                    "cpu_freq_mhz REAL, cpu_throttle_count INTEGER, pi_throttle_bits INTEGER",
    "disk_samples": "ts REAL NOT NULL, mount TEXT NOT NULL, used_pct REAL, free_gb REAL, "
                    "inode_used_pct REAL",
    "proc_samples": "ts REAL NOT NULL, pid INTEGER, name TEXT, cpu_pct REAL, rss_mb REAL",
    "thermal_zones": "ts REAL NOT NULL, zone TEXT NOT NULL, temp_c REAL",
    "power_samples": "ts REAL NOT NULL, rail TEXT NOT NULL, watts REAL, volts REAL, amps REAL",
    "tcp_samples": "ts REAL NOT NULL, retrans_segs INTEGER, out_rsts INTEGER, estab_resets INTEGER, "
                   "udp_in_errors INTEGER, udp_no_ports INTEGER, "
                   "conntrack_used INTEGER, conntrack_max INTEGER",
    "disk_health": "ts REAL NOT NULL, device TEXT NOT NULL, wear_pct REAL, ioerr_count INTEGER",
}
_IX = {"ping_runs": "target", "net_samples": "iface", "http_samples": "url", "mtr_hops": "target",
       "thermal_zones": "zone", "power_samples": "rail", "disk_health": "device"}

STD_TABLES = tuple(_BODY)  # generic append-only tables (id + body + node [+ src_id])


def columns(table: str) -> list[str]:
    """Body column names (excludes id/node/src_id)."""
    return [c.split()[0] for c in _BODY[table].split(",")]


def _node_ddl() -> str:
    parts = []
    for t, body in _BODY.items():
        parts.append(f"CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY, {body}, node TEXT);")
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_ts ON {t}(ts);")
        if t in _IX:
            parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_{_IX[t]}_ts ON {t}({_IX[t]}, ts);")
    parts.append("CREATE TABLE IF NOT EXISTS ping_rtts (run_id INTEGER NOT NULL, rtt_ms REAL NOT NULL);")
    parts.append("CREATE INDEX IF NOT EXISTS ix_ping_rtts_run ON ping_rtts(run_id);")
    return "\n".join(parts)


def _hub_ddl() -> str:
    parts = []
    for t, body in _BODY.items():
        parts.append(f"CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY, {body}, "
                     f"node TEXT, src_id INTEGER, UNIQUE(node, src_id));")
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_node_ts ON {t}(node, ts);")
    parts.append("CREATE TABLE IF NOT EXISTS ping_rtts (id INTEGER PRIMARY KEY, run_id INTEGER, rtt_ms REAL);")
    parts.append("CREATE INDEX IF NOT EXISTS ix_ping_rtts_run ON ping_rtts(run_id);")
    return "\n".join(parts)


def _safe_add_column(conn: sqlite3.Connection, table: str, col_ddl: str) -> bool:
    """ALTER TABLE ADD COLUMN that tolerates a concurrent add of the same column.

    Two collector daemons (collect fast, collect slow) plus iperf open the same node
    DB and each run the migration at startup. On an in-place upgrade they all see the
    same columns missing and race the ALTERs; the loser would otherwise hit
    `OperationalError: duplicate column name`, which is SQLITE_ERROR (not SQLITE_BUSY),
    so busy_timeout does not retry it and the daemon would crash before its scheduler
    starts. Swallow only that specific error; re-raise anything else. Returns True when
    this call added the column, False when it was already present."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_ddl}")
        return True
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            return False
        raise


def ensure_node_column(conn: sqlite3.Connection, tables=STD_TABLES) -> None:
    """Additive migration: add a `node` column to existing tables that lack it."""
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()]
        if cols and "node" not in cols and _safe_add_column(conn, t, "node TEXT"):
            conn.execute(f"UPDATE {t} SET node = ? WHERE node IS NULL", (config.NODE,))
    conn.commit()


def _body_cols(table: str) -> list[tuple[str, str]]:
    """Return [(name, full_ddl_fragment)] for each body column. Used by ensure_body_columns
    to detect and ALTER ADD missing columns on existing tables (additive migration)."""
    return [(c.strip().split()[0], c.strip()) for c in _BODY[table].split(",")]


def ensure_body_columns(conn: sqlite3.Connection, tables=STD_TABLES) -> None:
    """Additive migration: ALTER ADD any body columns missing from existing tables.
    Lets new columns (e.g. rtt_p25, rtt_p75) be introduced without breaking old DBs.
    Old rows keep NULL for the new columns; readers must tolerate that."""
    for t in tables:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        if not cols:
            continue
        for name, ddl in _body_cols(t):
            if name in cols:
                continue
            # Strip NOT NULL on retro-added cols since old rows have no value to backfill.
            ddl_safe = ddl.replace(" NOT NULL", "")
            _safe_add_column(conn, t, ddl_safe)
    conn.commit()


def init_node(conn: sqlite3.Connection) -> None:
    conn.executescript(_node_ddl())
    conn.commit()
    ensure_node_column(conn)
    ensure_body_columns(conn)


def init_hub(conn: sqlite3.Connection) -> None:
    conn.executescript(_hub_ddl())
    conn.commit()
    ensure_body_columns(conn)


_SQL_CACHE: dict[str, tuple[str, list[str]]] = {}


def _sql(table: str) -> tuple[str, list[str]]:
    """(insert_sql, body_cols), cached: _BODY is static so the SQL never changes.
    insert() is called several times per collect cycle, so skip re-parsing each time."""
    cached = _SQL_CACHE.get(table)
    if cached is None:
        cols = columns(table)
        sql = f"INSERT INTO {table} ({','.join(cols)},node) VALUES ({','.join('?' * (len(cols) + 1))})"
        cached = _SQL_CACHE[table] = (sql, cols)
    return cached


def insert(conn: sqlite3.Connection, table: str, rows: list[dict], node: str = config.NODE) -> None:
    """Generic INSERT of body columns + node. rows are dicts keyed by body column name."""
    sql, cols = _sql(table)
    conn.executemany(sql, [[r.get(c) for c in cols] + [node] for r in rows])


def insert_one(conn: sqlite3.Connection, table: str, row: dict, node: str = config.NODE) -> int:
    """INSERT a single row; return its rowid (used where a FK to it is needed, e.g. ping_runs)."""
    sql, cols = _sql(table)
    return conn.execute(sql, [row.get(c) for c in cols] + [node]).lastrowid
