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
    # write_mb_day is populated only on the self row (name='smokemon'): the fleet's projected
    # SD-write rate, so card wear is as visible as RSS. NULL for ordinary top-N proc rows.
    "proc_samples": "ts REAL NOT NULL, pid INTEGER, name TEXT, cpu_pct REAL, rss_mb REAL, write_mb_day REAL",
    "thermal_zones": "ts REAL NOT NULL, zone TEXT NOT NULL, temp_c REAL",
    "power_samples": "ts REAL NOT NULL, rail TEXT NOT NULL, watts REAL, volts REAL, amps REAL",
    "tcp_samples": "ts REAL NOT NULL, retrans_segs INTEGER, out_rsts INTEGER, estab_resets INTEGER, "
                   "udp_in_errors INTEGER, udp_no_ports INTEGER, "
                   "conntrack_used INTEGER, conntrack_max INTEGER",
    "disk_health": "ts REAL NOT NULL, device TEXT NOT NULL, wear_pct REAL, ioerr_count INTEGER",
    "synthetic_samples": "ts REAL NOT NULL, probe TEXT NOT NULL, ok INTEGER, latency_ms REAL, detail TEXT",
    "ext_metrics": "ts REAL NOT NULL, source TEXT NOT NULL, metric TEXT NOT NULL, value REAL, unit TEXT, labels TEXT",
    "ext_events": "ts REAL NOT NULL, source TEXT NOT NULL, severity TEXT, event TEXT NOT NULL, detail TEXT",
    "redis_samples": "ts REAL NOT NULL, instance TEXT NOT NULL, stream TEXT, connected INTEGER, "
                     "used_memory_mb REAL, xlen INTEGER, pending INTEGER, "
                     "connected_clients INTEGER, blocked_clients INTEGER, ops_per_sec REAL, "
                     "evicted_keys INTEGER, rejected_connections INTEGER",
    "gpu_samples": "ts REAL NOT NULL, gpu TEXT NOT NULL, util_pct REAL, freq_mhz REAL",
    "docker_samples": "ts REAL NOT NULL, name TEXT NOT NULL, image TEXT, state TEXT, running INTEGER, "
                      "health TEXT, exit_code INTEGER, restart_count INTEGER, oom_killed INTEGER, "
                      "cpu_pct REAL, mem_mb REAL, pids INTEGER",
    "proc_watch": "ts REAL NOT NULL, label TEXT NOT NULL, count INTEGER, cpu_pct REAL, rss_mb REAL, "
                  "uptime_s REAL, restarts INTEGER",
    "stream_probes": "ts REAL NOT NULL, url TEXT NOT NULL, ok INTEGER, latency_ms REAL, status TEXT",
    "tcp_checks": "ts REAL NOT NULL, name TEXT NOT NULL, host TEXT, port INTEGER, ok INTEGER, "
                  "latency_ms REAL, bytes INTEGER, detail TEXT",
    "port_samples": "ts REAL NOT NULL, proto TEXT NOT NULL, dir TEXT NOT NULL, port INTEGER NOT NULL, "
                    "conns INTEGER, peers INTEGER, listening INTEGER, bytes_sent INTEGER, bytes_recv INTEGER",
    # device/environment inventory (delta-coded: a row is written only when a fact's value
    # changes), so this carries "everything about the device and its environment" for ~zero
    # steady-state cost. kind groups facts for rendering (hw / os / net / runtime).
    "device_facts": "ts REAL NOT NULL, key TEXT NOT NULL, value TEXT, kind TEXT",
    # event-driven, capped, redacted log tails (opt-in). reason = what triggered the capture;
    # dropped = bytes skipped by the drop-oldest cap; excerpt = redacted text (wire-gzipped by
    # the shipper). Written only on incident, never as a stream.
    "log_excerpts": "ts REAL NOT NULL, source TEXT NOT NULL, path TEXT, reason TEXT, "
                    "bytes INTEGER, dropped INTEGER, excerpt TEXT",
}
_IX = {"ping_runs": "target", "net_samples": "iface", "http_samples": "url", "mtr_hops": "target",
       "thermal_zones": "zone", "power_samples": "rail", "disk_health": "device",
       "synthetic_samples": "probe", "ext_metrics": "source", "ext_events": "source",
       "redis_samples": "instance", "gpu_samples": "gpu", "docker_samples": "name",
       "proc_watch": "label", "stream_probes": "url", "tcp_checks": "name", "port_samples": "port",
       "device_facts": "key", "log_excerpts": "source"}

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
        # (node, ts): per-node time-range scans (the fleet/risks per-node loaders) + GROUP BY node.
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_node_ts ON {t}(node, ts);")
        # (ts): the dashboard runs many CROSS-node `WHERE ts >= ?` windows. Without a leading-ts
        # index those full-scan the table - a big reason a large hub DB makes GETs time out.
        parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_ts ON {t}(ts);")
        # (node, <entity>, ts): the "latest value per (node, entity)" queries - latest_metrics
        # (ping per target), /metrics, services (docker/redis/proc per name), inventory - become a
        # loose index scan that jumps to each group's max-ts tail instead of scanning all history.
        if t in _IX:
            parts.append(f"CREATE INDEX IF NOT EXISTS ix_{t}_node_{_IX[t]}_ts ON {t}(node, {_IX[t]}, ts);")
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


# ---------- hub-side rollups (downsampling) ----------

# The heavy time-series tables worth downsampling for long-window aggregate queries. The other
# tables are either tiny (device_facts, alert_state), latest-row only (docker/redis/proc_watch),
# or event-driven (ext_events, log_excerpts), so a rollup buys nothing there.
ROLLUP_TABLES = ("ping_runs", "host_samples", "net_samples", "tcp_samples", "wifi_samples")
ROLLUP_BUCKETS = {"_1m": 60, "_1h": 3600}

# How each numeric body column collapses within a bucket. Counters (kernel monotonics, byte
# gauges) MUST keep max so the per-second delta loaders (load_net/load_tcp/load_freq) still see a
# monotonically rising value across buckets; loss/bandwidth use max so a spike is never averaged
# away; everything else (levels, rates, latencies) uses mean. Text/identity columns use last.
_ROLLUP_MAX_COLS = {
    "loss_pct", "rtt_max", "ibytes", "obytes", "ipkts", "opkts",
    "retrans_segs", "out_rsts", "estab_resets", "udp_in_errors", "udp_no_ports",
    "conntrack_used", "conntrack_max", "retry_count", "discard_count", "beacon_loss",
    "oom_kill_count", "cpu_throttle_count", "pi_throttle_bits",
}
# Columns that are not aggregated numerically: the bucket's representative text value.
_ROLLUP_TEXT_COLS = {"ssid", "channel", "phy_mode", "bssid"}


def _col_type(table: str, col: str) -> str:
    """The declared SQL type of a body column (used to tell numeric from text)."""
    for name, ddl in _body_cols(table):
        if name == col:
            return ddl.upper()
    return ""


def rollup_select_cols(table: str) -> list[tuple[str, str]]:
    """[(out_col, sql_expr)] for building one rollup row from a group of raw rows. ts becomes the
    bucket start; the entity column is a GROUP BY key (kept verbatim); numeric cols aggregate;
    text cols take MAX (a stable representative). node is added by the caller."""
    entity = _IX.get(table)
    out: list[tuple[str, str]] = []
    for col in columns(table):
        if col == "ts":
            continue
        if col == entity:
            out.append((col, col))
        elif col in _ROLLUP_TEXT_COLS or "TEXT" in _col_type(table, col) or col in _ROLLUP_MAX_COLS:
            # text/identity columns take a representative MAX; counters/loss/bandwidth keep MAX so
            # the per-second delta loaders still see a monotonically rising value across buckets.
            out.append((col, f"MAX({col})"))
        else:
            out.append((col, f"AVG({col})"))
    return out


def _rollup_ddl() -> str:
    """CREATE for every <table><suffix> rollup (same body cols + node + bucket_ts) and the
    rollup_state cursor. bucket_ts is the bucket start epoch; (node, entity, bucket_ts) is unique
    so a re-run can INSERT OR IGNORE without duplicating a bucket."""
    parts = []
    for t in ROLLUP_TABLES:
        # Drop the raw `ts` column entirely (the rollup keys on bucket_ts) and relax NOT NULL on
        # the rest: a rolled-up bucket has no single raw ts, and aggregates of all-NULL columns
        # are NULL. Keeping `ts NOT NULL` would make INSERT OR IGNORE silently drop every bucket.
        body_cols = [c.strip() for c in _BODY[t].split(",") if c.strip().split()[0] != "ts"]
        body = ", ".join(c.replace(" NOT NULL", "") for c in body_cols)
        entity = _IX.get(t)
        uniq = f"UNIQUE(node, {entity}, bucket_ts)" if entity else "UNIQUE(node, bucket_ts)"
        for suffix in ROLLUP_BUCKETS:
            rt = t + suffix
            parts.append(f"CREATE TABLE IF NOT EXISTS {rt} (id INTEGER PRIMARY KEY, {body}, "
                         f"node TEXT, bucket_ts REAL NOT NULL, {uniq});")
            parts.append(f"CREATE INDEX IF NOT EXISTS ix_{rt}_node_ts ON {rt}(node, bucket_ts);")
            parts.append(f"CREATE INDEX IF NOT EXISTS ix_{rt}_ts ON {rt}(bucket_ts);")
    parts.append("CREATE TABLE IF NOT EXISTS rollup_state ("
                 "tbl TEXT NOT NULL, bucket TEXT NOT NULL, last_ts REAL NOT NULL DEFAULT 0, "
                 "PRIMARY KEY (tbl, bucket));")
    return "\n".join(parts)


def ensure_rollup_tables(conn: sqlite3.Connection) -> None:
    """Create the hub-side rollup tables + cursor (additive, IF NOT EXISTS)."""
    conn.executescript(_rollup_ddl())
    conn.commit()


def init_hub(conn: sqlite3.Connection) -> None:
    conn.executescript(_hub_ddl())
    # Per-POST ingest accounting (hub-only): actual compressed bytes received on the wire per
    # node, so the dashboard can show real ship volume (not a from-the-DB estimate). wire_bytes
    # = Content-Length of the gzipped body; raw_bytes = decompressed JSON size.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS ingest_log ("
        "ts REAL NOT NULL, node TEXT, wire_bytes INTEGER, raw_bytes INTEGER, rows INTEGER);"
        "CREATE INDEX IF NOT EXISTS ix_ingest_log_ts ON ingest_log (ts);")
    # Alert-delivery flap-suppression state (hub-only, not shipped): one row per currently-firing
    # service alert, keyed by "node/kind/label". notified_ts drives the re-notify cooldown; rows are
    # deleted when the alert clears. Tiny (a handful of rows), so no extra index. See alerts.py.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS alert_state ("
        "key TEXT PRIMARY KEY, node TEXT, kind TEXT, label TEXT, severity INTEGER, "
        "detail TEXT, first_ts REAL, notified_ts REAL, cleared_ts REAL);")
    # cleared_ts added later for resolve-linger flap suppression; migrate pre-existing tables.
    if "cleared_ts" not in {r[1] for r in conn.execute("PRAGMA table_info(alert_state)")}:
        conn.execute("ALTER TABLE alert_state ADD COLUMN cleared_ts REAL")
    conn.commit()
    ensure_body_columns(conn)
    ensure_rollup_tables(conn)
    # Give the planner stats so it picks the loose-index scan over a full (node,ts) scan for the
    # latest-value queries. analysis_limit caps the sampling so this stays cheap even on a large
    # hub DB (a few hundred index rows per table, not a full ANALYZE).
    try:
        conn.execute("PRAGMA analysis_limit=400")
        conn.execute("PRAGMA optimize")
    except sqlite3.OperationalError as e:
        # optimize is advisory; never let a stats hiccup stop the hub from starting.
        import sys
        print(f"[schema] PRAGMA optimize skipped: {e!r}", file=sys.stderr)


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
